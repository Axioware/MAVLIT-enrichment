"""
pipeline/enrichment/csv_partnership_check/csv_partnership_check.py

TEMP / one-off helper: take a CSV with an `instagram_url` column, check every
account in Meta's Branded Content Library (facebook_branded_content_checker.py)
and write the same CSV back with new columns telling whether the brand is
doing partnerships and whether it is already in brands_raw.

New column `partnership_brand`:
    true              — doing branded-content partnerships AND not in brands_raw
    already in table  — doing partnerships AND already in brands_raw
    false             — checked, no branded-content results in the window
    error             — the check failed (retry by running again)
    no instagram_url  — the row has no usable Instagram username
Extra columns: `instagram_username`, `branded_content_results`, `check_status`
(found / not_found / not_in_dropdown / error).

Read-only: nothing is written to brands_raw or any other table (brands_raw is
only read, to match the Instagram handle). The output file is written row by
row, so a stopped run keeps what was checked. Rows already carrying a result
in the output file are NOT reused — run again on the same input to refresh.

Run from the project root (needs FACEBOOK_PROXIES + DATABASE_URL in .env):
    python -m pipeline.enrichment.csv_partnership_check.csv_partnership_check \
        --input brands.csv --output brands_checked.csv
    python -m pipeline.enrichment.csv_partnership_check.csv_partnership_check \
        --input brands.csv --output brands_checked.csv --limit 5
"""

import argparse
import asyncio
import csv
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

from pipeline.db import BrandRaw, SessionLocal
from pipeline.enrichment_re.facebook_branded_content_checker import (
    END_DATE,
    START_DATE,
    check_branded_content,
)
from pipeline.helpers.social import normalize_handle

logger = logging.getLogger(__name__)

URL_COLUMN = "instagram_url"
NEW_COLUMNS = ["partnership_brand", "instagram_username", "branded_content_results", "check_status"]

TRUE = "true"
ALREADY = "already in table"
FALSE = "false"
ERROR = "error"
NO_URL = "no instagram_url"


def username_from_url(value: str | None) -> str | None:
    """https://www.instagram.com/gopro/ | instagram.com/gopro | @gopro | gopro -> 'gopro'."""
    text = (value or "").strip()
    if not text:
        return None
    if "instagram.com" in text.lower():
        if "//" not in text:
            text = "https://" + text
        text = urlparse(text).path.strip("/").split("/", 1)[0]
    text = text.strip().lstrip("@").split("?", 1)[0].strip("/")
    return text.lower() or None


def _known_usernames() -> set[str]:
    """Lower-cased Instagram usernames already in brands_raw (read-only)."""
    db = SessionLocal()
    try:
        return {
            normalize_handle(h).lower()
            for (h,) in db.query(BrandRaw.instagram_handle)
            .filter(BrandRaw.instagram_handle.isnot(None), BrandRaw.instagram_handle != "")
            .all()
            if normalize_handle(h)
        }
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", required=True, type=Path, help=f"CSV with an '{URL_COLUMN}' column.")
    parser.add_argument("--output", required=True, type=Path, help="CSV to write (input columns + new ones).")
    parser.add_argument("--limit", type=int, default=None, help="Only check the first N rows (testing).")
    parser.add_argument("--headed", action="store_true", help="Show the browser window (debugging).")
    args = parser.parse_args(argv)

    if not args.input.is_file():
        parser.error(f"--input not found: {args.input}")
    if args.output.resolve() == args.input.resolve():
        parser.error("--output must be a different file than --input")

    with args.input.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if URL_COLUMN not in (reader.fieldnames or []):
            parser.error(f"column '{URL_COLUMN}' not found; the CSV has: {', '.join(reader.fieldnames or [])}")
        fieldnames = list(reader.fieldnames) + [c for c in NEW_COLUMNS if c not in reader.fieldnames]
        rows = list(reader)
    if args.limit:
        rows = rows[:args.limit]

    known = _known_usernames()
    logger.info("%d row(s) to check; %d Instagram handle(s) already in brands_raw; window %s .. %s",
                len(rows), len(known), START_DATE, END_DATE)

    cache: dict[str, tuple[str, int | None]] = {}   # username -> (status, result_count)
    counts = {TRUE: 0, ALREADY: 0, FALSE: 0, ERROR: 0, NO_URL: 0}
    with args.output.open("w", newline="", encoding="utf-8") as out:
        writer = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for i, row in enumerate(rows, 1):
            username = username_from_url(row.get(URL_COLUMN))
            row["instagram_username"] = username or ""
            if not username:
                row.update(partnership_brand=NO_URL, branded_content_results="", check_status="")
                counts[NO_URL] += 1
            else:
                if username not in cache:
                    try:
                        r = asyncio.run(check_branded_content(username, headless=not args.headed))
                        status = "found" if r.result_count >= 1 else ("not_found" if r.instagram_id else "not_in_dropdown")
                        cache[username] = (status, r.result_count)
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:   # CheckError / proxy / browser — row marked error
                        logger.warning("[%d/%d] @%s: check failed (%s)", i, len(rows), username, exc)
                        cache[username] = ("error", None)
                status, count = cache[username]
                if status == "error":
                    verdict = ERROR
                elif (count or 0) > 0:
                    verdict = ALREADY if username in known else TRUE
                else:
                    verdict = FALSE
                row.update(partnership_brand=verdict, branded_content_results="" if count is None else count,
                           check_status=status)
                counts[verdict] += 1
                logger.info("[%d/%d] @%s: %s (%s, %s result(s))", i, len(rows), username, verdict, status,
                            "-" if count is None else count)
            writer.writerow(row)
            out.flush()

    print("\n" + "=" * 60)
    print(f"Written to {args.output}")
    print(f"  true (partnership, not in brands_raw): {counts[TRUE]}")
    print(f"  already in table (partnership):        {counts[ALREADY]}")
    print(f"  false (no partnership):                {counts[FALSE]}")
    print(f"  error (run again to retry):            {counts[ERROR]}")
    print(f"  no instagram_url:                      {counts[NO_URL]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
