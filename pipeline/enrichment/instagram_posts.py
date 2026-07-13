"""
pipeline/enrichment/instagram_posts.py

Scrapes recent Instagram posts for each brand via Apify (shu8hvrXbJbY3Eb9W).

 Saving logic 

ENABLE_INSTA_LLM = True  (full LLM mode)
  • ALL signals (paid_partnership, sponsors, taggedUsers, mentions,
    coauthorProducers) go through Mistral.
  • LLM filters out false positives and returns trimmed versions of each field.
  • Filtered values are stored back into their own columns.
  • llm_checked = True
  • Post is skipped if LLM removes all signals.

ENABLE_INSTA_LLM = False  (coauthor-only LLM mode)
  • paid_partnership, sponsors, taggedUsers, mentions → saved as-is from Apify.
  • coauthorProducers → ALWAYS filtered through Mistral (even when flag is off).
  • Filtered coauthors stored in coauthor_producers column.
  • llm_checked = False  (never True in this mode)
  • Post is skipped only if the ONLY signal is coauthorProducers AND LLM rejects all.

 Prompts (editable via /admin > Prompts) 
  instagram_post_full_check   — used when ENABLE_INSTA_LLM=True
  instagram_coauthor_check    — always used for coauthorProducers filtering
"""

import json
import logging
import time

from sqlalchemy.orm import Session

from config import APIFY_TOKEN, ENABLE_INSTA_LLM, MISTRAL_API_KEY
from pipeline.db import BrandRaw, InstagramPost, Prompt
from pipeline.helpers.apify import run_apify_actor
from pipeline.helpers.db import upsert_rows
from pipeline.helpers.llm import call_mistral_json, fill_template
from pipeline.helpers.prompts import (
    FULL_PROMPT_NAME, FULL_DEFAULT_PROMPT,
    COAUTHOR_PROMPT_NAME, COAUTHOR_DEFAULT_PROMPT,
)
from pipeline.helpers.social import normalize_handle

logger = logging.getLogger(__name__)

_ACTOR_ID     = "shu8hvrXbJbY3Eb9W"
_POSTS_NEWER  = "1 months"
_RESULTS_TYPE = "posts"

#  Prompt helpers

def _get_full_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == FULL_PROMPT_NAME).first()
    return row.content if row else FULL_DEFAULT_PROMPT


def _get_coauthor_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == COAUTHOR_PROMPT_NAME).first()
    return row.content if row else COAUTHOR_DEFAULT_PROMPT


def _fmt(value) -> str:
    """Serialize a list/dict/scalar to compact JSON for LLM prompts."""
    if value is None or value == [] or value == {}:
        return "none"
    return json.dumps(value, ensure_ascii=False)


def _strip_pic(items) -> list[dict] | None:
    """Remove profile_pic_url from each entry in a list of user dicts."""
    if not items or not isinstance(items, list):
        return items
    return [{k: v for k, v in entry.items() if k != "profile_pic_url"} for entry in items]


#  LLM functions 

def _llm_filter_all(db: Session, item: dict, brand_name: str) -> dict | None:
    """
    Full-filter mode (ENABLE_INSTA_LLM=True).
    Sends all signals to Mistral; returns dict with filtered field values.
    Returns None when MISTRAL_API_KEY is not set so the caller can skip the post.
    """
    if not MISTRAL_API_KEY:
        logger.warning("Instagram LLM full: MISTRAL_API_KEY not set — skipping post")
        return None

    caption = (item.get("caption") or "")[:600]
    prompt = fill_template(
        _get_full_prompt(db),
        brand_name=brand_name,
        caption=caption,
        paid_partnership=str(bool(item.get("paidPartnership"))).lower(),
        sponsors=_fmt(item.get("sponsors")),
        tagged_users=_fmt(item.get("taggedUsers")),
        mentions=_fmt(item.get("mentions")),
        coauthor_producers=_fmt(item.get("coauthorProducers")),
    )
    result = call_mistral_json(prompt, context=f"{brand_name} full-filter post {item.get('id')}")
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


def _llm_filter_coauthors(db: Session, item: dict, brand_name: str) -> list:
    """
    Coauthor-only filter — always active regardless of ENABLE_INSTA_LLM.
    Returns the filtered coauthor list (may be empty).
    Falls back to the raw Apify list when MISTRAL_API_KEY is not set.
    """
    raw = item.get("coauthorProducers") or []
    if not MISTRAL_API_KEY:
        logger.warning("Instagram LLM coauthor: MISTRAL_API_KEY not set — saving coauthors as-is")
        return raw

    caption = (item.get("caption") or "")[:600]
    prompt = fill_template(
        _get_coauthor_prompt(db),
        brand_name=brand_name,
        caption=caption,
        coauthor_producers=_fmt(raw),
    )
    result = call_mistral_json(prompt, context=f"{brand_name} coauthor-filter post {item.get('id')}")
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
    return {
        "brand_raw_id":           brand_raw_id,
        "instagram_handle":       handle,
        "post_id":                str(post_id),
        "short_code":             item.get("shortCode"),
        "post_url":               item.get("url"),
        "post_type":              item.get("type"),
        "timestamp":              item.get("timestamp"),
        "caption":                item.get("caption"),
        "hashtags":               item.get("hashtags"),
        "mentions":               item.get("mentions"),
        "tagged_users":           _strip_pic(item.get("taggedUsers")),
        "coauthor_producers":     _strip_pic(item.get("coauthorProducers")),
        "paid_partnership":       item.get("paidPartnership"),
        "sponsors":               item.get("sponsors"),
        "likes_count":            item.get("likesCount"),
        "comments_count":         item.get("commentsCount"),
        "video_view_count":       item.get("videoViewCount"),
        "video_play_count":       item.get("videoPlayCount"),
        "followers_count":        item.get("followersCount"),
        "follows_count":          item.get("followsCount"),
        "posts_count":            item.get("postsCount"),
        "is_business_account":    item.get("isBusinessAccount"),
        "verified":               item.get("verified"),
        "biography":              item.get("biography"),
        "external_url":           item.get("externalUrl"),
        "business_category_name": item.get("businessCategoryName"),
        "llm_checked":            False,
    }


#  Main enrichment function 

def enrich_instagram_posts(
    db: Session,
    limit: int = 50,
    posts_newer_than: str = _POSTS_NEWER,
    brand_id: int | None = None,
) -> int:
    """
    For each brand with instagram_handle set and instagram_checked=False:
      1. Scrape posts via Apify
      2. Apply LLM filtering based on ENABLE_INSTA_LLM flag
      3. Store qualifying posts in instagram_posts
      4. Mark instagram_checked=True

    Pass brand_id to target one specific brand directly — this bypasses the
    instagram_checked filter (so you can re-run/test a brand that was already
    processed), but instagram_handle must still be set.

    Returns number of brands processed.
    """
    if not APIFY_TOKEN:
        logger.warning("APIFY_TOKEN not set — skipping Instagram enrichment")
        return 0

    query = db.query(BrandRaw).filter(BrandRaw.instagram_handle.isnot(None))
    if brand_id is not None:
        query = query.filter(BrandRaw.id == brand_id)
    else:
        query = query.filter(BrandRaw.instagram_checked == False)

    brands: list[BrandRaw] = query.limit(limit).all()

    if not brands:
        logger.info("Instagram: no pending brands with instagram_handle")
        return 0

    logger.info(
        "Instagram: processing %d brands (ENABLE_INSTA_LLM=%s)",
        len(brands), ENABLE_INSTA_LLM,
    )
    total_posts = 0

    for brand in brands:
        handle = normalize_handle(brand.instagram_handle)
        items  = _scrape_handle(handle, posts_newer_than)

        inserted          = 0
        skipped_no_signal = 0
        skipped_llm       = 0

        for item in items:
            has_paid   = bool(item.get("paidPartnership") or item.get("sponsors"))
            has_coauth = bool(item.get("coauthorProducers"))
            has_social = bool(item.get("taggedUsers") or item.get("mentions"))

            if ENABLE_INSTA_LLM:
                #  Full LLM mode: all signals filtered 
                if not (has_paid or has_coauth or has_social):
                    skipped_no_signal += 1
                    continue

                filtered = _llm_filter_all(db, item, brand.name)
                if filtered is None:          # no API key
                    skipped_llm += 1
                    continue

                any_remaining = (
                    filtered.get("paid_partnership")
                    or filtered.get("sponsors")
                    or filtered.get("tagged_users")
                    or filtered.get("mentions")
                    or filtered.get("coauthor_producers")
                )
                if not any_remaining:
                    skipped_llm += 1
                    continue

                row = _build_row(brand.id, handle, item)
                if row:
                    row["paid_partnership"]   = bool(filtered.get("paid_partnership"))
                    row["sponsors"]           = filtered.get("sponsors") or None
                    row["tagged_users"]       = filtered.get("tagged_users") or None
                    row["mentions"]           = filtered.get("mentions") or None
                    row["coauthor_producers"] = filtered.get("coauthor_producers") or None
                    row["llm_checked"]        = True
                    inserted += upsert_rows(db, InstagramPost, [row], ["post_id"])
                time.sleep(0.3)

            else:
                #  Coauthor-only LLM mode 
                # Direct signals (paid/sponsors/tagged/mentions) saved as-is.
                # coauthorProducers always goes through LLM.
                has_direct = has_paid or has_social

                filtered_coauthors: list | None = None
                if has_coauth:
                    filtered_coauthors = _llm_filter_coauthors(db, item, brand.name)
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
                    inserted += upsert_rows(db, InstagramPost, [row], ["post_id"])

        total_posts += inserted
        logger.info(
            "Instagram: '%s' (@%s) → %d saved | %d no signal | %d LLM-rejected | %d total fetched",
            brand.name, handle, inserted, skipped_no_signal, skipped_llm, len(items),
        )

        brand.instagram_checked = True
        db.commit()
        time.sleep(1.0)

    logger.info("Instagram: %d brands processed, %d posts stored", len(brands), total_posts)
    return len(brands)


def _scrape_handle(handle: str, posts_newer_than: str) -> list[dict]:
    """Run Apify actor for one Instagram handle and return raw items."""
    logger.info("Instagram: scraping @%s (newer than %s)", handle, posts_newer_than)
    run_input = {
        "addParentData":      True,
        "directUrls":         [f"https://www.instagram.com/{handle}/"],
        "onlyPostsNewerThan": posts_newer_than,
        "resultsType":        _RESULTS_TYPE,
    }
    return run_apify_actor(_ACTOR_ID, run_input, label=f"Instagram @{handle}")
