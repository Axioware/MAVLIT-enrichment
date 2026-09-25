"""Embed a test MAVLIT user profile and score it against creator_niches.

Edit MAVLIT_USER_NICHE / MAVLIT_USER_DESCRIPTION / MAVLIT_USER_TAGS below to the
profile you want to test, then run:
    .venv/bin/python script_creator_niche/mavlit_user_similarity.py
"""

import csv
import logging
import sys
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from pgvector.sqlalchemy import Vector
from sqlalchemy import bindparam, create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from config import DATABASE_URL
from pipeline.helpers.gpt_llm import embed_text


# --- Edit this test MAVLIT user profile before running -----------------
MAVLIT_USER_NICHE = "Beauty"
MAVLIT_USER_DESCRIPTION = "Strength training and gym motivation content for everyday athletes."
MAVLIT_USER_TAGS = ["fitness", "gym", "strength training", "workouts", "motivation"]
# -------------------------------------------------------------------------

OUTPUT_PATH = PROJECT_ROOT / "mavlit_user_similarity.csv"
CSV_COLUMNS = ("username", "niche", "description", "tags", "cosine_similarity")
EMBEDDING_DIMENSIONS = 1024
FLOAT_TOLERANCE = 1e-9
logger = logging.getLogger(__name__)


SIMILARITY_QUERY = text("""
SELECT
    username,
    niche,
    description,
    tags,
    1 - (embedding <=> :query_vector) AS cosine_similarity
FROM creator_niches
WHERE embedding IS NOT NULL
ORDER BY cosine_similarity DESC
""").bindparams(bindparam("query_vector", type_=Vector(EMBEDDING_DIMENSIONS)))


def _format_tags(tags: object) -> str:
    if isinstance(tags, list):
        return ", ".join(str(tag) for tag in tags if str(tag).strip())
    return ""


def _similarity_value(value: object, username: str) -> float:
    if value is None:
        raise ValueError(f"Null similarity for username {username!r}")

    similarity = float(value)
    if similarity < -FLOAT_TOLERANCE or similarity > 1 + FLOAT_TOLERANCE:
        raise ValueError(f"Similarity out of range for username {username!r}: {similarity}")

    # Guard against tiny database floating-point drift at the boundaries.
    return min(1.0, max(0.0, similarity))


def _build_mavlit_user_text() -> str:
    niche = (MAVLIT_USER_NICHE or "").strip()
    description = (MAVLIT_USER_DESCRIPTION or "").strip()
    if not niche:
        raise ValueError("MAVLIT_USER_NICHE must not be empty")
    if not description:
        raise ValueError("MAVLIT_USER_DESCRIPTION must not be empty")
    if not isinstance(MAVLIT_USER_TAGS, list):
        raise ValueError("MAVLIT_USER_TAGS must be a list of strings")
    return f"Niche: {niche}\nDescription: {description}\nTags: {_format_tags(MAVLIT_USER_TAGS)}"


def generate_csv() -> tuple[int, int]:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")

    mavlit_user_text = _build_mavlit_user_text()
    logger.info("Embedding MAVLIT test user: %s", MAVLIT_USER_NICHE)
    query_vector = embed_text(mavlit_user_text, context="mavlit_user_similarity")
    if not query_vector:
        raise RuntimeError("Failed to embed the MAVLIT test user profile")

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            creator_count = int(
                connection.execute(
                    text("SELECT COUNT(*) FROM creator_niches WHERE embedding IS NOT NULL")
                ).scalar_one()
            )
            logger.info("Found %d creator niches with embeddings", creator_count)

            rows = []
            for row in connection.execute(SIMILARITY_QUERY, {"query_vector": query_vector}):
                username = row.username
                if not username:
                    raise ValueError("A creator_niches row is missing a username")

                similarity = _similarity_value(row.cosine_similarity, username)
                rows.append({
                    "username": username,
                    "niche": row.niche or "",
                    "description": row.description or "",
                    "tags": _format_tags(row.tags),
                    "cosine_similarity": f"{similarity:.6f}",
                })

        if len(rows) != creator_count:
            raise ValueError(f"Expected {creator_count} similarity rows, generated {len(rows)}")

        for row in rows:
            if not row["username"]:
                raise ValueError("Generated CSV contains a missing username")
            value = Decimal(row["cosine_similarity"])
            if value < 0 or value > 1:
                raise ValueError(f"Generated CSV contains an out-of-range similarity: {value}")

        with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

        logger.info("Generated %d similarity row(s)", len(rows))
        logger.info("CSV created: %s", OUTPUT_PATH)
        return creator_count, len(rows)
    finally:
        engine.dispose()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        creator_count, row_count = generate_csv()
    except Exception:
        logger.exception("Could not generate MAVLIT user similarity CSV")
        return 1

    print(f"Creator niches scored: {creator_count}")
    print(f"Generated rows: {row_count}")
    print(f"Output: {OUTPUT_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
