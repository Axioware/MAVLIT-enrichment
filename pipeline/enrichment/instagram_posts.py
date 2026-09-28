"""
pipeline/enrichment/instagram_posts.py

Scrapes recent Instagram posts for each brand via Apify (shu8hvrXbJbY3Eb9W).

 Saving logic 

ENABLE_INSTA_LLM = True  (full LLM mode)
  • ALL signals (paid_partnership, sponsors, taggedUsers, mentions,
    coauthorProducers) go through the LLM.
  • LLM filters out false positives and returns trimmed versions of each field.
  • Filtered values are stored back into their own columns.
  • llm_checked = True
  • Post is skipped unless a real creator survives filtering — sponsors,
    tagged_users, mentions, or coauthor_producers. paid_partnership is just
    a boolean flag and does not count as a creator on its own.

ENABLE_INSTA_LLM = False  (coauthor-only LLM mode)
  • paid_partnership, sponsors, taggedUsers, mentions → saved as-is from Apify.
  • coauthorProducers → ALWAYS filtered through the LLM (even when flag is off).
  • Filtered coauthors stored in coauthor_producers column.
  • llm_checked = False  (never True in this mode)
  • Post is skipped unless a real creator is present — sponsors, tagged_users,
    mentions, or a confirmed coauthor. paid_partnership alone is not enough.

 Prompts (editable via /admin > Prompts)
  instagram_post_full_check   — used when ENABLE_INSTA_LLM=True; this already
                                 covers coauthorProducers as one of its signals
  instagram_coauthor_check    — only used when ENABLE_INSTA_LLM=False, since
                                 instagram_post_full_check doesn't run then
"""

import json
import logging
import time

from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session

from config import APIFY_TOKEN, ENABLE_INSTA_LLM, OPENAI_KEY
from pipeline.db import BrandRaw, InstagramPost, Prompt
from pipeline.helpers.apify import ApifyQuotaExceeded, run_apify_actor
from pipeline.helpers.db import upsert_rows
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template
from pipeline.helpers.prompts import (
    FULL_PROMPT_NAME, FULL_DEFAULT_PROMPT,
    COAUTHOR_PROMPT_NAME, COAUTHOR_DEFAULT_PROMPT,
    INSTAGRAM_POST_SPONSORSHIP_PROMPT_NAME,
    INSTAGRAM_POST_SPONSORSHIP_DEFAULT_PROMPT,
)
from pipeline.helpers.social import normalize_handle

logger = logging.getLogger(__name__)

_ACTOR_ID     = "shu8hvrXbJbY3Eb9W"
_POSTS_LIMIT  = 40   # last N posts, regardless of how far back that goes
_RESULTS_TYPE = "posts"

#  Prompt helpers

def _get_full_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == FULL_PROMPT_NAME).first()
    return row.content if row else FULL_DEFAULT_PROMPT


def _get_coauthor_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == COAUTHOR_PROMPT_NAME).first()
    return row.content if row else COAUTHOR_DEFAULT_PROMPT


def _get_sponsorship_confidence_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(
        Prompt.name == INSTAGRAM_POST_SPONSORSHIP_PROMPT_NAME
    ).first()
    return row.content if row else INSTAGRAM_POST_SPONSORSHIP_DEFAULT_PROMPT


def _fmt(value) -> str:
    """Serialize a list/dict/scalar to compact JSON for LLM prompts."""
    if value is None or value == [] or value == {}:
        return "none"
    return json.dumps(value, ensure_ascii=False)


def _usernames_only(items) -> list[str] | None:
    """
    Reduce a list down to just usernames — entries may be Apify's raw user
    dicts (id/username/full_name/is_verified/...) or, when this runs on an
    LLM's filtered response, plain username strings if the LLM didn't echo
    the full dict shape back. Handles both so only bare usernames ever reach
    the DB, never the LLM's id/full_name/is_verified fields.
    """
    if not items or not isinstance(items, list):
        return None
    usernames = []
    for entry in items:
        if isinstance(entry, dict) and entry.get("username"):
            usernames.append(entry["username"])
        elif isinstance(entry, str) and entry.strip():
            usernames.append(entry.strip())
    return usernames or None


def _real_coauthors(item: dict, brand_handle: str) -> list[dict]:
    """
    Apify's coauthorProducers lists a collab post's co-authors, which
    excludes whoever is the post's primary owner. When the brand is the
    co-author rather than the owner (a creator posted and tagged the brand),
    coauthorProducers only contains the brand's own account — the real
    collaborating creator is item["ownerUsername"] instead. Filter the
    brand's own username out of coauthorProducers and add the owner in
    when they're a different account.
    """
    handle_lower = (brand_handle or "").lower()
    candidates = [
        entry for entry in (item.get("coauthorProducers") or [])
        if isinstance(entry, dict) and (entry.get("username") or "").lower() != handle_lower
    ]
    owner = item.get("ownerUsername")
    if owner and owner.lower() != handle_lower and not any(
        (c.get("username") or "").lower() == owner.lower() for c in candidates
    ):
        candidates.append({"username": owner})
    return candidates


#  LLM functions 

def _llm_filter_all(db: Session, item: dict, brand_name: str, handle: str) -> dict | None:
    """
    Full-filter mode (ENABLE_INSTA_LLM=True).
    Sends all signals to the LLM; returns dict with filtered field values.
    Returns None when OPENAI_KEY is not set so the caller can skip the post.
    """
    if not OPENAI_KEY:
        logger.warning("Instagram LLM full: OPENAI_KEY not set — skipping post")
        return None

    caption = (item.get("caption") or "")[:600]
    prompt = fill_template(
        _get_full_prompt(db),
        brand_name=brand_name,
        caption=caption,
        paid_partnership=str(bool(item.get("paidPartnership"))).lower(),
        sponsors=_fmt(item.get("sponsors")),
        tagged_users=_fmt(_usernames_only(item.get("taggedUsers"))),
        mentions=_fmt(item.get("mentions")),
        coauthor_producers=_fmt(_usernames_only(_real_coauthors(item, handle))),
    )
    result = call_gpt_json(prompt, context=f"{brand_name} full-filter post {item.get('id')}")
    if not isinstance(result, dict):
        return {}
    logger.info(
        "Instagram LLM full: post %s — paid=%s sponsors=%d tagged=%d mentions=%d coauthors=%d",
        item.get("id"),
        result.get("paid_partnership"),
        len(result.get("sponsors") or []),
        len(result.get("tagged_users") or []),
        len(result.get("mentions") or []),
        len(result.get("coauthor_producers") or []),
    )
    return result


def _llm_filter_coauthors(db: Session, item: dict, brand_name: str, handle: str) -> list:
    """
    Coauthor-only filter — always active regardless of ENABLE_INSTA_LLM.
    Returns the filtered coauthor list (may be empty).
    Falls back to the raw Apify list when OPENAI_KEY is not set.
    """
    raw = _real_coauthors(item, handle)
    if not OPENAI_KEY:
        logger.warning("Instagram LLM coauthor: OPENAI_KEY not set — saving coauthors as-is")
        return raw

    caption = (item.get("caption") or "")[:600]
    prompt = fill_template(
        _get_coauthor_prompt(db),
        brand_name=brand_name,
        caption=caption,
        coauthor_producers=_fmt(raw),
    )
    result = call_gpt_json(prompt, context=f"{brand_name} coauthor-filter post {item.get('id')}")
    if not isinstance(result, dict):
        return []
    filtered = result.get("coauthor_producers", [])
    if not isinstance(filtered, list):
        filtered = []
    logger.info(
        "Instagram LLM coauthor: post %s → %d/%d confirmed for '%s'",
        item.get("id"), len(filtered), len(raw), brand_name,
    )
    return filtered


#  Row builder 

def _build_row(brand_raw_id: int, handle: str, item: dict) -> dict | None:
    """Map a raw Apify item to an instagram_posts row. Returns None if no post_id."""
    post_id = item.get("id")
    if not post_id:
        return None
    # Profile-level fields (followers, bio, etc.) are nested under metaData —
    # they belong to the post's author profile, not the post itself.
    meta = item.get("metaData") or {}

    return {
        "brand_raw_id":           brand_raw_id,
        "instagram_handle":       handle,
        "post_id":                str(post_id),
        "post_url":               item.get("url"),
        "post_type":              item.get("type"),
        "timestamp":              item.get("timestamp"),
        "caption":                item.get("caption"),
        "hashtags":               item.get("hashtags"),
        "mentions":               item.get("mentions"),
        "tagged_users":           _usernames_only(item.get("taggedUsers")),
        "coauthor_producers":     _usernames_only(_real_coauthors(item, handle)),
        "paid_partnership":       item.get("paidPartnership"),
        "sponsors":               item.get("sponsors"),
        "likes_count":            item.get("likesCount"),
        "comments_count":         item.get("commentsCount"),
        "video_view_count":       item.get("videoViewCount"),
        "video_play_count":       item.get("videoPlayCount"),
        "followers_count":        meta.get("followersCount"),
        "follows_count":          meta.get("followsCount"),
        "posts_count":            meta.get("postsCount"),
        "is_business_account":    meta.get("isBusinessAccount"),
        "verified":               meta.get("verified"),
        "biography":              meta.get("biography"),
        "external_url":           meta.get("externalUrl"),
        "business_category_name": meta.get("businessCategoryName"),
        "llm_checked":            False,
    }


def _score_post_sponsorship_confidence(
    prompt_template: str,
    brand_name: str,
    row: dict,
) -> int | None:
    prompt = fill_template(
        prompt_template,
        brand_name=brand_name or "unknown",
        caption=(row.get("caption") or "")[:600] or "none",
        paid_partnership=str(bool(row.get("paid_partnership"))).lower(),
        sponsors=_fmt(row.get("sponsors")),
        tagged_users=_fmt(row.get("tagged_users")),
        mentions=_fmt(row.get("mentions")),
        coauthor_producers=_fmt(row.get("coauthor_producers")),
    )
    result = call_gpt_json(
        prompt,
        context=f"instagram post sponsorship score {brand_name} {row.get('post_id')}",
    )
    confidence = result.get("confidence_pct") if isinstance(result, dict) else None
    if not isinstance(confidence, (int, float)):
        return None
    return max(0, min(100, round(confidence)))


def _save_post_with_confidence(
    db: Session,
    brand_name: str,
    row: dict,
    prompt_template: str | None,
) -> int:
    inserted = upsert_rows(db, InstagramPost, [row], ["post_id"])
    if not prompt_template:
        return inserted

    stored_post = db.query(InstagramPost).filter(
        InstagramPost.post_id == row["post_id"]
    ).first()
    if stored_post is None or stored_post.sponsorship_confidence is not None:
        return inserted

    confidence = _score_post_sponsorship_confidence(
        prompt_template,
        brand_name,
        {
            "post_id": stored_post.post_id,
            "caption": stored_post.caption,
            "paid_partnership": stored_post.paid_partnership,
            "sponsors": stored_post.sponsors,
            "tagged_users": stored_post.tagged_users,
            "mentions": stored_post.mentions,
            "coauthor_producers": stored_post.coauthor_producers,
        },
    )
    if confidence is not None:
        stored_post.sponsorship_confidence = confidence
        db.commit()
    return inserted


def score_instagram_post_sponsorship(
    db: Session,
    limit: int | None = None,
    brand_raw_id: int | None = None,
) -> int:
    """Score pending saved Instagram posts that contain creator references."""
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping Instagram post sponsorship scoring")
        return 0

    prompt_template = _get_sponsorship_confidence_prompt(db)
    query = (
        db.query(InstagramPost)
        .filter(InstagramPost.sponsorship_confidence.is_(None))
        .filter(
            or_(
                InstagramPost.sponsors.isnot(None),
                InstagramPost.tagged_users.isnot(None),
                InstagramPost.mentions.isnot(None),
                InstagramPost.coauthor_producers.isnot(None),
            )
        )
    )
    if brand_raw_id is not None:
        query = query.filter(InstagramPost.brand_raw_id == brand_raw_id)
    if limit is not None:
        query = query.limit(limit)
    rows: list[InstagramPost] = query.all()

    if not rows:
        logger.info("Instagram post sponsorship scoring: no rows pending")
        return 0

    logger.info("Instagram post sponsorship scoring: processing %d row(s)", len(rows))
    updated = 0
    failed = 0
    for row in rows:
        brand_name = row.brand_raw.name if row.brand_raw else row.instagram_handle
        score = _score_post_sponsorship_confidence(
            prompt_template,
            brand_name,
            {
                "post_id": row.post_id,
                "caption": row.caption,
                "paid_partnership": row.paid_partnership,
                "sponsors": row.sponsors,
                "tagged_users": row.tagged_users,
                "mentions": row.mentions,
                "coauthor_producers": row.coauthor_producers,
            },
        )
        if score is None:
            logger.warning(
                "Instagram post sponsorship scoring: id=%d @%s — LLM call failed",
                row.id, row.instagram_handle,
            )
            failed += 1
            time.sleep(0.5)
            continue

        row_id, row_handle = row.id, row.instagram_handle
        row.sponsorship_confidence = score
        db.commit()
        updated += 1
        logger.info(
            "Instagram post sponsorship scoring: id=%d @%s → %d%%",
            row_id, row_handle, score,
        )
        time.sleep(0.5)

    logger.info("Instagram post sponsorship scoring: %d updated, %d failed", updated, failed)
    return updated


def _build_profile_only_row(brand_raw_id: int, handle: str, items: list[dict]) -> dict:
    """
    Row for a brand whose scrape returned zero posts worth keeping (no
    sponsorship signal survived filtering, or the account had no posts at
    all) — keeps the brand's profile snapshot (followers, bio, etc.)
    without any post-specific fields, so every checked brand still has at
    least one instagram_posts row to read a follower count from.

    Profile fields come from the first raw item's metaData (addParentData
    embeds the same profile snapshot on every item), since a filtered-out
    item still carries it. items may be empty (account had zero posts at
    all) — falls back to no profile data in that case.
    """
    meta = (items[0].get("metaData") or {}) if items else {}
    return {
        "brand_raw_id":           brand_raw_id,
        "instagram_handle":       handle,
        "followers_count":        meta.get("followersCount"),
        "follows_count":          meta.get("followsCount"),
        "posts_count":            meta.get("postsCount"),
        "is_business_account":    meta.get("isBusinessAccount"),
        "verified":               meta.get("verified"),
        "biography":              meta.get("biography"),
        "external_url":           meta.get("externalUrl"),
        "business_category_name": meta.get("businessCategoryName"),
    }


#  Main enrichment function

def enrich_instagram_posts(
    db: Session,
    limit: int = 50,
    posts_limit: int = _POSTS_LIMIT,
    brand_id: int | None = None,
    niche: str | None = None,
) -> int:
    """
    For each brand with instagram_handle set, instagram_checked=False,
    refferls=False, geo_reach_score NULL or 0-40, and
    has_official_website=True:
      1. Scrape posts via Apify
      2. Apply LLM filtering based on ENABLE_INSTA_LLM flag
      3. Store qualifying posts in instagram_posts
      4. Mark instagram_checked=True

    posts_limit caps the scrape at the brand's last N posts (resultsLimit),
    regardless of how far back that goes — no time-window filter is applied.
    All posts_limit posts are always fetched from Apify in one call (no
    change to scrape cost).

    Pass brand_id to target one specific brand directly — this bypasses the
    has_official_website filter, but still skips the brand when
    instagram_checked=True. instagram_handle must still be set.

    Pass niche to scope the run to brands.niche matching that value exactly
    (case-insensitive) — brands_raw.niche is stored verbatim as typed at
    seed time (see pipeline/seed.py), so this must match that same string.
    Ignored if brand_id is also given.

    Returns number of brands processed.
    """
    if not APIFY_TOKEN:
        logger.warning("APIFY_TOKEN not set — skipping Instagram enrichment")
        return 0

    query = db.query(BrandRaw).filter(
        BrandRaw.instagram_handle.isnot(None),
        BrandRaw.refferls.is_(False),
        (BrandRaw.geo_reach_score.is_(None) | BrandRaw.geo_reach_score.between(0, 40)),
    )
    if brand_id is not None:
        query = query.filter(
            BrandRaw.id == brand_id,
            BrandRaw.instagram_checked.is_(False),
        )
    else:
        query = query.filter(
            BrandRaw.instagram_checked.is_(False),
            BrandRaw.has_official_website.is_(True),
        )
        if niche:
            query = query.filter(func.lower(BrandRaw.niche) == niche.strip().lower())

    brands: list[BrandRaw] = query.limit(limit).all()

    if not brands:
        logger.info("Instagram: no pending brands with instagram_handle")
        return 0

    logger.info(
        "Instagram: processing %d brands (ENABLE_INSTA_LLM=%s)",
        len(brands), ENABLE_INSTA_LLM,
    )
    total_posts = 0
    checked_count = 0

    try:
        for brand in brands:
            handle = normalize_handle(brand.instagram_handle)
            items  = _scrape_handle(handle, posts_limit)

            if items is None:
                logger.warning(
                    "Instagram: '%s' (@%s) — Apify scrape failed (actor limit reached, network "
                    "error, etc.) — leaving instagram_checked=False so this brand is retried "
                    "instead of being treated as fully processed",
                    brand.name, handle,
                )
                time.sleep(1.0)
                continue

            sponsorship_prompt = (
                _get_sponsorship_confidence_prompt(db) if OPENAI_KEY else None
            )
            inserted          = 0
            skipped_no_signal = 0
            skipped_llm       = 0

            for item in items:
                # sponsors/tagged_users/mentions/coauthor_producers are the only
                # fields that actually name a creator — paid_partnership is just
                # a boolean flag Apify sets on the post and never identifies who
                # the partner is, so it must never by itself justify saving a row.
                has_sponsors = bool(item.get("sponsors"))
                has_coauth   = bool(_real_coauthors(item, handle))
                has_social   = bool(item.get("taggedUsers") or item.get("mentions"))

                if ENABLE_INSTA_LLM:
                    #  Full LLM mode: all signals filtered
                    if not (has_sponsors or has_coauth or has_social):
                        skipped_no_signal += 1
                        continue

                    filtered = _llm_filter_all(db, item, brand.name, handle)
                    if filtered is None:          # no API key
                        skipped_llm += 1
                        continue

                    has_creator = (
                        filtered.get("sponsors")
                        or filtered.get("tagged_users")
                        or filtered.get("mentions")
                        or filtered.get("coauthor_producers")
                    )
                    if not has_creator:
                        skipped_llm += 1
                        continue

                    row = _build_row(brand.id, handle, item)
                    if row:
                        # tagged_users/coauthor_producers/mentions: the LLM is
                        # handed Apify's raw dicts (id/username/full_name/
                        # is_verified) so it can judge who's a real creator, but
                        # it also echoes that same dict shape back — reduce to
                        # bare usernames here so only usernames ever land in the
                        # DB, same as when ENABLE_INSTA_LLM is off.
                        row["paid_partnership"]   = bool(filtered.get("paid_partnership"))
                        row["sponsors"]           = filtered.get("sponsors") or None
                        row["tagged_users"]       = _usernames_only(filtered.get("tagged_users"))
                        row["mentions"]           = _usernames_only(filtered.get("mentions"))
                        row["coauthor_producers"] = _usernames_only(filtered.get("coauthor_producers"))
                        row["llm_checked"]        = True
                        inserted += _save_post_with_confidence(
                            db, brand.name, row, sponsorship_prompt
                        )
                    time.sleep(0.3)

                else:
                    #  Coauthor-only LLM mode
                    # Direct signals (sponsors/tagged/mentions) saved as-is.
                    # coauthorProducers always goes through LLM.
                    has_direct = has_sponsors or has_social

                    filtered_coauthors: list | None = None
                    if has_coauth:
                        filtered_coauthors = _usernames_only(_llm_filter_coauthors(db, item, brand.name, handle))
                        time.sleep(0.3)

                    if not has_direct and not filtered_coauthors:
                        if has_coauth:
                            skipped_llm += 1
                        else:
                            skipped_no_signal += 1
                        continue

                    row = _build_row(brand.id, handle, item)
                    if row:
                        if filtered_coauthors is not None:
                            row["coauthor_producers"] = filtered_coauthors or None
                        # llm_checked stays False in this mode
                        inserted += _save_post_with_confidence(
                            db, brand.name, row, sponsorship_prompt
                        )

            if inserted == 0:
                upsert_rows(
                    db, InstagramPost,
                    [_build_profile_only_row(brand.id, handle, items)],
                    ["brand_raw_id"], index_where=text("post_id IS NULL"),
                )

            total_posts += inserted
            logger.info(
                "Instagram: '%s' (@%s) → %d saved | %d no signal | %d LLM-rejected | "
                "%d total fetched",
                brand.name, handle, inserted, skipped_no_signal, skipped_llm,
                len(items),
            )

            brand.instagram_checked = True
            db.commit()
            checked_count += 1
            time.sleep(1.0)
    except ApifyQuotaExceeded as exc:
        logger.error(
            "Instagram: %s — stopping this run early after %d/%d brand(s) checked; "
            "remaining brands stay instagram_checked=False for the next run",
            exc, checked_count, len(brands),
        )

    logger.info("Instagram: %d/%d brands checked, %d posts stored", checked_count, len(brands), total_posts)
    # checked_count, not len(brands) — a batch where every brand hits an
    # Apify failure (e.g. actor limit reached) must return 0 so
    # drain_pending_step's `while True: ... if not processed: break` stops
    # this step instead of looping forever re-selecting the same still-
    # unchecked brands every batch.
    return checked_count


def _scrape_handle(handle: str, posts_limit: int) -> list[dict] | None:
    """
    Run Apify actor for one Instagram handle and return raw items.
    require_success=True so a failed run (actor limit reached, network
    error, actor crash, etc.) returns None — distinguishable from a
    genuinely empty [] result, which enrich_instagram_posts() needs to
    avoid marking a brand instagram_checked=True off the back of a call
    that never actually ran.
    """
    logger.info("Instagram: scraping @%s (last %d posts)", handle, posts_limit)
    run_input = {
        "addParentData": True,
        "directUrls":    [f"https://www.instagram.com/{handle}/"],
        "resultsLimit":  posts_limit,
        "resultsType":   _RESULTS_TYPE,
    }
    return run_apify_actor(_ACTOR_ID, run_input, label=f"Instagram @{handle}", require_success=True)
