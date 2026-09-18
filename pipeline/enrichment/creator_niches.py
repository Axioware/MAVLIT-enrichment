"""Generate concise, evidence-based niche descriptions for Instagram creators."""

import argparse
import logging
import os
import sys
import time
from collections import defaultdict

if __package__ in (None, ""):
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from pipeline.db import CreatorNiche, InstagramUser, Prompt, SessionLocal
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template
from pipeline.helpers.prompts import (
    CREATOR_NICHE_DESCRIPTION_DEFAULT_PROMPT,
    CREATOR_NICHE_DESCRIPTION_PROMPT_NAME,
)

logger = logging.getLogger(__name__)

_DEFAULT_BATCH_SIZE = 25
_DEFAULT_RETRIES = 3
_MAX_CAPTIONS = 10


def _get_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == CREATOR_NICHE_DESCRIPTION_PROMPT_NAME).first()
    return row.content if row else CREATOR_NICHE_DESCRIPTION_DEFAULT_PROMPT


def _caption_text(row: InstagramUser) -> str:
    return (row.caption or "").strip()


def _build_context(rows: list[InstagramUser]) -> dict[str, dict[str, object]]:
    """Group source rows by username and retain the five newest captions."""
    by_username: dict[str, list[InstagramUser]] = defaultdict(list)
    for row in rows:
        by_username[row.username].append(row)

    contexts: dict[str, dict[str, object]] = {}
    for username, username_rows in by_username.items():
        ordered = sorted(
            username_rows,
            key=lambda row: (row.post_timestamp is not None, row.post_timestamp or "", row.id or 0),
            reverse=True,
        )
        captions = [_caption_text(row) for row in ordered if _caption_text(row)][: _MAX_CAPTIONS]
        contexts[username] = {
            "bio": next((row.bio.strip() for row in ordered if row.bio and row.bio.strip()), ""),
            "niche": next((row.niche.strip() for row in ordered if row.niche and row.niche.strip()), ""),
            "captions": captions,
        }
    return contexts


def _validate_result(result: object) -> dict[str, object] | None:
    if not isinstance(result, dict):
        return None

    description = result.get("description")
    tags = result.get("tags")
    if not isinstance(description, str) or not description.strip():
        return None
    if not isinstance(tags, list):
        return None

    clean_tags: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if not isinstance(tag, str):
            continue
        clean_tag = " ".join(tag.split())
        key = clean_tag.casefold()
        if clean_tag and key not in seen:
            seen.add(key)
            clean_tags.append(clean_tag)

    description = " ".join(description.split())
    if len(description.split()) > 35 or not 5 <= len(clean_tags) <= 10:
        return None
    return {"description": description, "tags": clean_tags}


def _generate_niche(
    db: Session,
    username: str,
    context: dict[str, object],
    retries: int,
) -> dict[str, object] | None:
    prompt = fill_template(
        _get_prompt(db),
        niche=context["niche"] or "none provided",
        bio=context["bio"] or "none provided",
        captions="\n".join(f"- {caption}" for caption in context["captions"]) or "none provided",
    )
    for attempt in range(1, retries + 1):
        result = _validate_result(
            call_gpt_json(prompt, context=f"creator niche @{username} attempt={attempt}")
        )
        if result:
            return result
        if attempt < retries:
            delay = min(8.0, 2 ** (attempt - 1))
            logger.warning("Creator niche: retrying @%s in %.1fs", username, delay)
            time.sleep(delay)
    logger.error("Creator niche: failed to generate valid result for @%s", username)
    return None


def run_creator_niches(
    db: Session,
    limit: int | None = None,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    retries: int = _DEFAULT_RETRIES,
    instagram_user_id: int | None = None,
) -> int:
    """Generate missing profiles without modifying existing output rows."""
    eligible = db.query(InstagramUser).filter(
        InstagramUser.user_type != "commenter",
        InstagramUser.niche.isnot(None),
        InstagramUser.niche != "",
        or_(InstagramUser.bio.isnot(None), InstagramUser.caption.isnot(None)),
    )
    if instagram_user_id is not None:
        selected_row = db.query(InstagramUser.username).filter(
            InstagramUser.id == instagram_user_id,
        ).first()
        if not selected_row:
            logger.warning("Creator niche: instagram_user_id=%d not found", instagram_user_id)
            return 0
        username_key = selected_row.username.strip().casefold()
        eligible = eligible.filter(
            func.lower(func.trim(InstagramUser.username)) == username_key,
        )
    eligible = eligible.order_by(InstagramUser.id.asc()).all()
    contexts = _build_context(eligible)
    processed_usernames = {
        username.strip().casefold()
        for (username,) in db.query(CreatorNiche.username).all()
        if username and username.strip()
    }
    pending = []
    seen_usernames: set[str] = set()
    for row in eligible:
        username_key = row.username.strip().casefold()
        if username_key in processed_usernames or username_key in seen_usernames:
            continue
        seen_usernames.add(username_key)
        pending.append(row)
    if limit is not None:
        pending = pending[:limit]
    logger.info("Creator niche: %d eligible, %d pending, %d username context(s)", len(eligible), len(pending), len(contexts))

    created = 0
    for batch_start in range(0, len(pending), batch_size):
        batch = pending[batch_start : batch_start + batch_size]
        inserts = []
        for row in batch:
            niche = _generate_niche(db, row.username, contexts[row.username], retries)
            if not niche:
                continue
            inserts.append({
                "instagram_user_id": row.id,
                "username": row.username,
                "niche": row.niche.strip(),
                "description": niche["description"],
                "tags": niche["tags"],
            })
        if inserts:
            from sqlalchemy.dialects.postgresql import insert
            stmt = insert(CreatorNiche).values(inserts).on_conflict_do_nothing()
            try:
                result = db.execute(stmt)
                db.commit()
                created += result.rowcount
            except Exception:
                db.rollback()
                logger.exception("Creator niche: failed to insert batch starting at %d", batch_start)
    logger.info("Creator niche: created=%d", created)
    return created


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s")
    parser = argparse.ArgumentParser(description="Generate Instagram creator niche descriptions and tags.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of source rows to process.")
    parser.add_argument("--instagram-user-id", type=int, default=None, help="Process one instagram_users.id.")
    parser.add_argument("--batch-size", type=int, default=_DEFAULT_BATCH_SIZE, help="Number of profiles per database batch.")
    parser.add_argument("--retries", type=int, default=_DEFAULT_RETRIES, help="LLM attempts per creator.")
    args = parser.parse_args()
    if args.batch_size < 1 or args.retries < 1:
        parser.error("--batch-size and --retries must be positive")
    db = SessionLocal()
    try:
        created = run_creator_niches(
            db,
            limit=args.limit,
            batch_size=args.batch_size,
            retries=args.retries,
            instagram_user_id=args.instagram_user_id,
        )
        print(f"creator_niches: created={created}")
    finally:
        db.close()


if __name__ == "__main__":
    main()