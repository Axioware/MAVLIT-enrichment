"""Add and populate 1024-dimensional embeddings for test_niches.
    .venv/bin/python scripts/embed_test_niches.py --force
"""

import argparse
import logging
from pathlib import Path
import sys

from dotenv import load_dotenv
from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, MetaData, Table, Text, bindparam, create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from config import DATABASE_URL
from pipeline.helpers.gpt_llm import embed_text_batch


DEFAULT_BATCH_SIZE = 100
EMBEDDING_DIMENSIONS = 1024
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate embeddings for every eligible niche, including existing embeddings.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Number of niches per embedding request (default: {DEFAULT_BATCH_SIZE}).",
    )
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be greater than zero")
    return args


def build_table() -> Table:
    metadata = MetaData()
    return Table(
        "test_niches",
        metadata,
        Column("niche", Text, primary_key=True),
        Column("description", Text),
        Column("embedding", Vector(EMBEDDING_DIMENSIONS)),
    )


def ensure_embedding_column(engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.execute(text(
            "ALTER TABLE test_niches ADD COLUMN IF NOT EXISTS embedding VECTOR(1024)"
        ))


def load_niches(engine, force: bool) -> list[dict[str, str]]:
    predicate = "" if force else "AND embedding IS NULL"
    query = text(f"""
        SELECT niche, description
        FROM test_niches
        WHERE niche IS NOT NULL
          AND TRIM(niche) <> ''
          AND description IS NOT NULL
          AND TRIM(description) <> ''
          {predicate}
        ORDER BY LOWER(niche)
    """)
    with engine.connect() as connection:
        return [dict(row._mapping) for row in connection.execute(query)]


def save_embeddings(engine, table: Table, rows: list[dict], vectors: list[list[float]]) -> None:
    update = (
        table.update()
        .where(table.c.niche == bindparam("niche_key"))
        .values(embedding=bindparam("embedding_value"))
    )
    values = [
        {"niche_key": row["niche"], "embedding_value": vector}
        for row, vector in zip(rows, vectors)
    ]
    with engine.begin() as connection:
        connection.execute(update, values)


def run(force: bool, batch_size: int) -> int:
    if not DATABASE_URL:
        logger.error("DATABASE_URL is not set")
        return 1

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    table = build_table()
    try:
        ensure_embedding_column(engine)
        rows = load_niches(engine, force)
        logger.info("Found %d niches requiring embeddings", len(rows))

        if not rows:
            logger.info("Successfully embedded 0 niches")
            logger.info("Failed: 0")
            return 0

        total_batches = (len(rows) + batch_size - 1) // batch_size
        succeeded = 0
        failed = 0
        for batch_number, start in enumerate(range(0, len(rows), batch_size), 1):
            batch = rows[start:start + batch_size]
            logger.info("Generating embeddings batch %d/%d", batch_number, total_batches)
            inputs = [f"Niche: {row['niche']}\nDescription: {row['description']}" for row in batch]
            vectors = embed_text_batch(inputs, context=f"test_niches batch {batch_number}/{total_batches}")

            if len(vectors) != len(batch):
                failed += len(batch)
                for row in batch:
                    logger.error("Failed embedding niche: %s", row["niche"])
                continue

            try:
                save_embeddings(engine, table, batch, vectors)
            except Exception:
                failed += len(batch)
                logger.exception("Failed saving embeddings batch %d/%d", batch_number, total_batches)
                for row in batch:
                    logger.error("Failed embedding niche: %s", row["niche"])
                continue

            succeeded += len(batch)
            for row in batch:
                logger.info("Embedded niche: %s", row["niche"])

        logger.info("Successfully embedded %d niches", succeeded)
        logger.info("Failed: %d", failed)
        return 1 if failed else 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(run(**vars(parse_args())))