"""
main.py — CLI entrypoint for the seed stage.

Usage:
    python main.py --niche fashion
    python main.py --niche music --google
    python main.py --niche fashion --limit 50   # for testing

"""

import argparse
import logging
import sys

from sqlalchemy import text

from pipeline.db import Base, SessionLocal, engine
from pipeline.seed import run_seed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)


def ensure_tables():
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        migrations = [
            "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS enrichment_failed BOOLEAN NOT NULL DEFAULT false",
            "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS country TEXT",
            "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS headquarters TEXT",
            "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS location TEXT",
            "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS operating_area TEXT",
        ]
        for sql in migrations:
            conn.execute(text(sql))
        conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Seed brands_raw from free sources")
    parser.add_argument("--niche", required=True, help="Niche to seed (e.g. fashion)")
    parser.add_argument(
        "--google",
        action="store_true",
        default=False,
        help="Enable Google SERP scraping via Playwright (slow, may be blocked)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap number of inserts — useful for smoke-testing",
    )
    args = parser.parse_args()

    ensure_tables()

    db = SessionLocal()
    try:
        inserted = run_seed(
            niche=args.niche,
            db=db,
            use_google=args.google,
            limit=args.limit,
        )
        print(f"\nDone. Inserted {inserted} new brands for niche: {args.niche}")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()