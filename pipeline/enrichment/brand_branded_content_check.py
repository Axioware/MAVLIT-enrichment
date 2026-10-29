"""
pipeline/enrichment/brand_branded_content_check.py

Do these brands run branded-content / paid partnerships?

Checks the Instagram account of brands in brands_raw that are NOT linked to
test_creator_brand_partnership_posts (added by discover_collaborator_creators.py or by
hand) against Meta's Branded Content Library (facebook_branded_content_checker.py) and
saves the outcome in facebook_branded_content_checks (admin: "Facebook Branded Content
Checks") - username, status, result_count (0 included), checked_at. result_count > 0
means the brand published branded-content / paid-partnership posts in the checker's
date window.

RUN CONDITIONS - a brands_raw row is checked when ALL hold:
  1. instagram_handle is set.
  2. It has no row in test_creator_brand_partnership_posts.
  3. Its username is not in facebook_branded_content_checks yet - or only with
     status 'error' (the check failed earlier, so it is retried). Brands that share a
     username are checked once.
--recheck ignores 3 and checks every such username again (overwriting its row);
--brand-id targets one brand (skips 3).

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
    BrandRaw,
    FacebookBrandedContentCheck,
    BatchSessionLocal,
    TestCreatorBrandPartnershipPost,
    engine,
)
from pipeline.enrichment_re.facebook_branded_content_checker import END_DATE, START_DATE, check_branded_content
from pipeline.helpers.social import normalize_handle

logger = logging.getLogger(__name__)

_STOP_AFTER_ERRORS = 8   # consecutive failed checks that stop the run (blocked proxy, layout change, ...)


def _pending_usernames(db: Session, recheck: bool, brand_id: int | None, limit: int | None) -> list[tuple[str, BrandRaw]]:
    """(username, first brand using it) for every username to check, in brand id order."""
    query = db.query(BrandRaw).filter(
        BrandRaw.instagram_handle.isnot(None),
        func.btrim(BrandRaw.instagram_handle) != "",
        ~exists().where(TestCreatorBrandPartnershipPost.brand_raw_id == BrandRaw.id),
    )
    if brand_id is not None:
        query = query.filter(BrandRaw.id == brand_id)
    done: set[str] = set()
    if brand_id is None and not recheck:
        done = {
            u.lower() for (u,) in db.query(FacebookBrandedContentCheck.username)
            .filter(FacebookBrandedContentCheck.status != "error")
        }
    pending: dict[str, BrandRaw] = {}
    for brand in query.order_by(BrandRaw.id):
        username = (normalize_handle(brand.instagram_handle) or "").lower()
        if username and username not in done and username not in pending:
            pending[username] = brand
            if limit and len(pending) >= limit:
                break
    return list(pending.items())


def _save_check(db: Session, username: str, status: str, instagram_id: str | None = None,
                result_count: int | None = None, error: str | None = None) -> None:
    values = {"username": username, "instagram_id": instagram_id, "status": status, "result_count": result_count,
              "error": (error or "")[:2000] or None, "start_date": START_DATE, "end_date": END_DATE}
    stmt = insert(FacebookBrandedContentCheck).values(**values)
    db.execute(stmt.on_conflict_do_update(
        index_elements=[FacebookBrandedContentCheck.username],
        set_={**{k: stmt.excluded[k] for k in values if k != "username"}, "checked_at": func.now()},
    ))
    db.commit()


def run(db: Session, limit: int | None = None, recheck: bool = False, brand_id: int | None = None,
        headed: bool = False, dry_run: bool = False) -> dict:
    """Check the pending usernames; returns {"checked", "found", "errors"}."""
    Base.metadata.create_all(bind=engine, tables=[FacebookBrandedContentCheck.__table__])
    todo = _pending_usernames(db, recheck, brand_id, limit)
    logger.info("Branded content check: %d username(s) to check%s", len(todo), " (dry run)" if dry_run else "")
    summary = {"checked": 0, "found": 0, "errors": 0}
    if dry_run:
        for username, brand in todo:
            logger.info("  would check @%s (brand id=%s %s)", username, brand.id, brand.name or "(no name)")
        return summary

    consecutive_errors = 0
    for i, (username, brand) in enumerate(todo, 1):
        tag = f"[{i}/{len(todo)}] {brand.name or '(no name)'} (@{username})"
        try:
            r = asyncio.run(check_branded_content(username, headless=not headed))
        except KeyboardInterrupt:
            raise
        except Exception as exc:   # CheckError / proxy / browser - recorded, retried next run
            logger.warning("%s: check failed (%s)", tag, exc)
            _save_check(db, username, "error", error=f"{type(exc).__name__}: {exc}")
            summary["errors"] += 1
            consecutive_errors += 1
            if consecutive_errors >= _STOP_AFTER_ERRORS:
                logger.error("Branded content check: %d checks failed in a row - stopping (rerun to continue)", consecutive_errors)
                break
            continue
        consecutive_errors = 0
        status = "found" if r.result_count >= 1 else ("not_found" if r.instagram_id else "not_in_dropdown")
        _save_check(db, username, status, instagram_id=r.instagram_id, result_count=r.result_count)
        summary["checked"] += 1
        summary["found"] += status == "found"
        logger.info("%s: %s - %d branded-content result(s)", tag, status, r.result_count)

    logger.info("Branded content check done: %s", summary)
    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Branded-content check for brands without test partnership posts.")
    parser.add_argument("--limit", type=int, default=None, help="Max number of usernames to check in this run.")
    parser.add_argument("--brand-id", type=int, default=None, dest="brand_id", help="Check one brands_raw.id.")
    parser.add_argument("--recheck", action="store_true", help="Check usernames again even if already in the table.")
    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    parser.add_argument("--dry-run", action="store_true", help="List what would be checked, change nothing.")
    args = parser.parse_args()
    with BatchSessionLocal() as db:
        run(db, limit=args.limit, recheck=args.recheck, brand_id=args.brand_id, headed=args.headed, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
