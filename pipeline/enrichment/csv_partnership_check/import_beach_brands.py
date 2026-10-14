"""Add checked beach brands with branded-content results to brands_raw.

Run from the project root:
    python -m pipeline.enrichment.csv_partnership_check.import_beach_brands
    python -m pipeline.enrichment.csv_partnership_check.import_beach_brands --dry-run
"""

import argparse
import csv
import logging
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert

from pipeline.db import BrandRaw, SessionLocal
from pipeline.helpers.normalize import normalize
from pipeline.helpers.social import normalize_handle

logger = logging.getLogger(__name__)
DEFAULT_INPUT = Path(__file__).with_name("beach_related_brands_checked.csv")


def _read_candidates(input_path: Path) -> tuple[list[dict[str, str]], int, int]:
    candidates: list[dict[str, str]] = []
    qualified = 0
    skipped = 0
    seen_handles: set[str] = set()
    seen_names: set[str] = set()

    with input_path.open(newline="", encoding="utf-8-sig") as source:
        reader = csv.DictReader(line.replace("\x00", "") for line in source)
        required_columns = {"brand_name", "instagram_username", "branded_content_results"}
        missing = required_columns - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing required column(s): {', '.join(sorted(missing))}")

        for row in reader:
            try:
                result_count = int((row.get("branded_content_results") or "").strip())
            except ValueError:
                continue
            if result_count <= 0:
                continue
            qualified += 1

            name = (row.get("brand_name") or "").strip()
            username = normalize_handle((row.get("instagram_username") or "").strip()).lower()
            normalized_name = normalize(name) if name else ""
            if not name or not username or not normalized_name:
                skipped += 1
                continue
            if username in seen_handles or normalized_name in seen_names:
                skipped += 1
                continue

            seen_handles.add(username)
            seen_names.add(normalized_name)
            candidates.append({
                "name": name,
                "normalized": normalized_name,
                "username": username,
            })

    return candidates, qualified, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Checked CSV to import.")
    parser.add_argument("--dry-run", action="store_true", help="Report inserts without writing to the database.")
    args = parser.parse_args(argv)

    if not args.input.is_file():
        parser.error(f"input CSV not found: {args.input}")

    try:
        candidates, qualified, skipped = _read_candidates(args.input)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    db = SessionLocal()
    try:
        existing = db.query(BrandRaw.instagram_handle, BrandRaw.name_normalized).all()
        existing_handles = {
            normalize_handle(handle).lower()
            for handle, _ in existing
            if handle and normalize_handle(handle)
        }
        existing_names = {name for _, name in existing if name}

        to_insert = [
            candidate for candidate in candidates
            if candidate["username"] not in existing_handles
            and candidate["normalized"] not in existing_names
        ]
        already_present = len(candidates) - len(to_insert)

        print(f"Rows with branded_content_results > 0: {qualified}")
        print(f"Skipped missing/duplicate CSV rows:    {skipped}")
        print(f"Already present in brands_raw:         {already_present}")
        print(f"New brands to add:                     {len(to_insert)}")

        if args.dry_run:
            return 0

        added = 0
        for candidate in to_insert:
            statement = (
                insert(BrandRaw)
                .values(
                    name=candidate["name"],
                    name_normalized=candidate["normalized"],
                    instagram_handle=f"https://www.instagram.com/{candidate['username']}/",
                    brand_tier="midhigher-range",
                )
                .on_conflict_do_nothing()
                .returning(BrandRaw.id)
            )
            inserted_id = db.execute(statement).scalar_one_or_none()
            if inserted_id is not None:
                added += 1
                existing_handles.add(candidate["username"])
                existing_names.add(candidate["normalized"])

        db.commit()
        print(f"Brands added:                          {added}")
    except Exception:
        db.rollback()
        logger.exception("Failed importing beach brands")
        raise
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    raise SystemExit(main())