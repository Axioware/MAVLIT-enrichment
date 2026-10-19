"""
pipeline/enrichment/brand_branded_content_check.py

Do these brands run branded-content / paid partnerships?

Takes the brands in brands_raw that are NOT linked to
test_creator_brand_partnership_posts - brands added by
discover_collaborator_creators.py or by hand - and checks each brand's Instagram
account against Meta's Branded Content Library (facebook_branded_content_checker.py).
Every checked brand gets a row in brand_branded_content_checks - brand name,
Instagram username, status, result_count (0 included), last_run - so you can see
which brands partner with creators (result_count > 0) and which do not.

RUN CONDITIONS - a brands_raw row is checked when ALL hold:
  1. instagram_handle is set.
  2. It has no row in test_creator_brand_partnership_posts.
  3. It has no row in brand_branded_content_checks yet, or the last check ended
     in 'error' (retried). --recheck re-checks every brand; --brand-id targets one
     brand (skips 3).
Usernames already checked for the same date window in facebook_branded_content_checks
(the table discover_collaborator_creators.py writes) are reused without opening the
browser, and every new check is saved there too.

Needs FACEBOOK_PROXIES (and Playwright) in .env, like the other branded-content scripts.
Run from the project root:
    python -m pipeline.enrichment.brand_branded_content_check --dry-run
    python -m pipeline.enrichment.brand_branded_content_check --limit 20
    python -m pipeline.enrichment.brand_branded_content_check
    python -m pipeline.enrichment.brand_branded_content_check --brand-id 123 --recheck
"""

import argparse
import asyncio
import logging

from sqlalchemy import exists, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from pipeline.db import (
    Base,
    BrandBrandedContentCheck,
    BrandRaw,
    FacebookBrandedContentCheck,
    SessionLocal,
    TestCreatorBrandPartnershipPost,
    engine,
)
from pipeline.enrichment_re.facebook_branded_content_checker import END_DATE, START_DATE, check_branded_content
from pipeline.helpers.social import normalize_handle

logger = logging.getLogger(__name__)

_REUSABLE_STATUSES = ("found", "not_found", "not_in_dropdown")   # not "error"
_STOP_AFTER_ERRORS = 8   # consecutive failed checks that stop the run (blocked proxy, layout change, ...)


def _pending_brands(db: Session, recheck: bool, brand_id: int | None, limit: int | None) -> list[BrandRaw]:
    query = db.query(BrandRaw).filter(
        BrandRaw.instagram_handle.isnot(None),
        func.btrim(BrandRaw.instagram_handle) != "",
        ~exists().where(TestCreatorBrandPartnershipPost.brand_raw_id == BrandRaw.id),
    )
    if brand_id is not None:
        query = query.filter(BrandRaw.id == brand_id)
    elif not recheck:
        done = exists().where(
            BrandBrandedContentCheck.brand_raw_id == BrandRaw.id,
            BrandBrandedContentCheck.status != "error",
        )
        query = query.filter(~done)
    query = query.order_by(BrandRaw.id)
    return (query.limit(limit) if limit else query).all()


def _saved_username_check(db: Session, username: str) -> dict | None:
    row = (
        db.query(FacebookBrandedContentCheck)
        .filter(
            FacebookBrandedContentCheck.username == username,
            FacebookBrandedContentCheck.status.in_(_REUSABLE_STATUSES),
            FacebookBrandedContentCheck.start_date == START_DATE,
            FacebookBrandedContentCheck.end_date == END_DATE,
        )
        .first()
    )
    if row is None:
        return None
    return {"status": row.status, "instagram_id": row.instagram_id, "result_count": row.result_count or 0}


def _save_username_check(db: Session, username: str, status: str, instagram_id: str | None,
                         result_count: int | None, error: str | None = None) -> None:
    """Keep facebook_branded_content_checks (one row per username) in step with this run."""
    values = {"username": username, "instagram_id": instagram_id, "status": status, "result_count": result_count,
              "error": (error or "")[:2000] or None, "start_date": START_DATE, "end_date": END_DATE}
    stmt = insert(FacebookBrandedContentCheck).values(**values)
    db.execute(stmt.on_conflict_do_update(
        index_elements=[FacebookBrandedContentCheck.username],
        set_={**{k: stmt.excluded[k] for k in values if k != "username"}, "checked_at": func.now()},
    ))
    db.commit()


def _save_brand_check(db: Session, brand: BrandRaw, username: str, status: str,
                      result_count: int | None, error: str | None = None) -> None:
    values = {"brand_raw_id": brand.id, "brand_name": brand.name, "instagram_handle": username, "status": status,
              "result_count": result_count, "error": (error or "")[:2000] or None,
              "start_date": START_DATE, "end_date": END_DATE}
    stmt = insert(BrandBrandedContentCheck).values(**values)
    db.execute(stmt.on_conflict_do_update(
        index_elements=[BrandBrandedContentCheck.brand_raw_id],
        set_={**{k: stmt.excluded[k] for k in values if k != "brand_raw_id"}, "last_run": func.now()},
    ))
    db.commit()


def run(db: Session, limit: int | None = None, recheck: bool = False, brand_id: int | None = None,
        headed: bool = False, dry_run: bool = False) -> dict:
    """Check the pending brands; returns {"checked", "reused", "found", "errors"}."""
    Base.metadata.create_all(bind=engine, tables=[FacebookBrandedContentCheck.__table__, BrandBrandedContentCheck.__table__])
    brands = _pending_brands(db, recheck, brand_id, limit)
    logger.info("Branded content check: %d brand(s) to check%s", len(brands), " (dry run)" if dry_run else "")
    summary = {"checked": 0, "reused": 0, "found": 0, "errors": 0}
    if dry_run:
        for b in brands:
            logger.info("  would check id=%s %s (@%s)", b.id, b.name or "(no name)", normalize_handle(b.instagram_handle))
        return summary

    seen: dict[str, dict] = {}
    consecutive_errors = 0
    for i, brand in enumerate(brands, 1):
        username = normalize_handle(brand.instagram_handle)
        tag = f"[{i}/{len(brands)}] {brand.name or '(no name)'} (@{username})"
        if not username:
            continue
        res = seen.get(username) or (None if recheck else _saved_username_check(db, username))
        if res:
            logger.info("%s: already checked (%s, %d result(s)) - reusing", tag, res["status"], res["result_count"])
            _save_brand_check(db, brand, username, res["status"], res["result_count"])
            seen[username] = res
            summary["reused"] += 1
            summary["found"] += res["status"] == "found"
            continue
        try:
            r = asyncio.run(check_branded_content(username, headless=not headed))
        except KeyboardInterrupt:
            raise
        except Exception as exc:   # CheckError / proxy / browser - recorded, retried next run
            logger.warning("%s: check failed (%s)", tag, exc)
            _save_brand_check(db, brand, username, "error", None, f"{type(exc).__name__}: {exc}")
            summary["errors"] += 1
            consecutive_errors += 1
            if consecutive_errors >= _STOP_AFTER_ERRORS:
                logger.error("Branded content check: %d checks failed in a row - stopping (rerun to continue)", consecutive_errors)
                break
            continue
        consecutive_errors = 0
        status = "found" if r.result_count >= 1 else ("not_found" if r.instagram_id else "not_in_dropdown")
        _save_username_check(db, username, status, r.instagram_id, r.result_count)
        _save_brand_check(db, brand, username, status, r.result_count)
        seen[username] = {"status": status, "instagram_id": r.instagram_id, "result_count": r.result_count}
        summary["checked"] += 1
        summary["found"] += status == "found"
        logger.info("%s: %s - %d branded-content result(s)", tag, status, r.result_count)

    logger.info("Branded content check done: %s", summary)
    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Branded-content check for brands without test partnership posts.")
    parser.add_argument("--limit", type=int, default=None, help="Max number of brands to check in this run.")
    parser.add_argument("--brand-id", type=int, default=None, dest="brand_id", help="Check one brands_raw.id.")
    parser.add_argument("--recheck", action="store_true", help="Re-check brands that already have a result.")
    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    parser.add_argument("--dry-run", action="store_true", help="List the brands that would be checked, change nothing.")
    args = parser.parse_args()
    with SessionLocal() as db:
        run(db, limit=args.limit, recheck=args.recheck, brand_id=args.brand_id, headed=args.headed, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
