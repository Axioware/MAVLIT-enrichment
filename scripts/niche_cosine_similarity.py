"""Generate pairwise test_niches cosine similarities above the threshold.
    .venv/bin/python scripts/niche_cosine_similarity.py
"""

import csv
import logging
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from config import DATABASE_URL


OUTPUT_PATH = PROJECT_ROOT / "niche_cosine_similarity.csv"
CSV_COLUMNS = ("source_niche", "target_niche", "cosine_similarity")
MIN_SIMILARITY = 0.55
FLOAT_TOLERANCE = 1e-9
logger = logging.getLogger(__name__)


COUNT_QUERY = text("""
SELECT COUNT(*)
FROM test_niches
WHERE embedding IS NOT NULL
""")


PAIR_COUNT_QUERY = text("""
SELECT COUNT(*)
FROM test_niches AS a
CROSS JOIN test_niches AS b
WHERE a.embedding IS NOT NULL
    AND b.embedding IS NOT NULL
    AND 1 - (a.embedding <=> b.embedding) > :min_similarity
""")


PAIR_QUERY = text("""
SELECT
    a.niche AS source_niche,
    b.niche AS target_niche,
    1 - (a.embedding <=> b.embedding) AS cosine_similarity
FROM test_niches AS a
CROSS JOIN test_niches AS b
WHERE a.embedding IS NOT NULL
  AND b.embedding IS NOT NULL
    AND 1 - (a.embedding <=> b.embedding) > :min_similarity
ORDER BY
    LOWER(a.niche),
    cosine_similarity DESC,
    LOWER(b.niche)
""")


def _similarity_value(value: object, source_niche: str, target_niche: str) -> float:
    if value is None:
        raise ValueError(f"Null similarity for {source_niche!r} -> {target_niche!r}")

    similarity = float(value)
    if similarity < -FLOAT_TOLERANCE or similarity > 1 + FLOAT_TOLERANCE:
        raise ValueError(
            f"Similarity out of range for {source_niche!r} -> {target_niche!r}: {similarity}"
        )

    # Guard against tiny database floating-point drift at the boundaries.
    return min(1.0, max(0.0, similarity))


def generate_csv() -> tuple[int, int]:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")

    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            niche_count = int(connection.execute(COUNT_QUERY).scalar_one())
            expected_pairs = int(
                connection.execute(
                    PAIR_COUNT_QUERY,
                    {"min_similarity": MIN_SIMILARITY},
                ).scalar_one()
            )
            logger.info("Found %d niches with embeddings", niche_count)
            logger.info(
                "Calculating pairwise cosine similarities above %.2f",
                MIN_SIMILARITY,
            )

            rows = []
            for row in connection.execute(
                PAIR_QUERY,
                {"min_similarity": MIN_SIMILARITY},
            ):
                source_niche = row.source_niche
                target_niche = row.target_niche
                if not source_niche or not target_niche:
                    raise ValueError("A pair contains a missing source_niche or target_niche")

                similarity = _similarity_value(
                    row.cosine_similarity,
                    source_niche,
                    target_niche,
                )
                rows.append({
                    "source_niche": source_niche,
                    "target_niche": target_niche,
                    "cosine_similarity": f"{similarity:.6f}",
                })

        if len(rows) != expected_pairs:
            raise ValueError(
                f"Expected {expected_pairs} similarity pairs, generated {len(rows)}"
            )

        for row in rows:
            if not row["source_niche"] or not row["target_niche"]:
                raise ValueError("Generated CSV contains a missing niche name")
            value = Decimal(row["cosine_similarity"])
            if value < 0 or value > 1:
                raise ValueError(f"Generated CSV contains an out-of-range similarity: {value}")

        with OUTPUT_PATH.open("w", newline="", encoding="utf-8") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

        logger.info("Generated %d similarity pairs", len(rows))
        logger.info("CSV created: %s", OUTPUT_PATH)
        return niche_count, len(rows)
    finally:
        engine.dispose()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        niche_count, pair_count = generate_csv()
    except Exception:
        logger.exception("Could not generate niche cosine-similarity CSV")
        return 1

    print(f"Niches: {niche_count}")
    print(f"Minimum similarity: {MIN_SIMILARITY:.2f}")
    print(f"Expected pairs: {pair_count}")
    print(f"Generated pairs: {pair_count}")
    print(f"Output: {OUTPUT_PATH.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
