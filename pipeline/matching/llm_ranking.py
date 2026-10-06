"""
pipeline/matching/llm_ranking.py

Matches v2 — LLM ranking of a creator's brands, as an alternative to the
rule-based v1 scoring in scoring.py.

Takes every brand that passes the same Stage 3 hard filters as v1
(matcher.hard_filtered_brands, nearest-first by embedding, capped at
_MAX_BRANDS) and asks the LLM to rate each one's sponsorship fit 0-100
against the creator's profile:

  creator — niche(s), sub-niche tags, content_tags, description(s),
            embedding_text
  brand   — name, niche, description, brands_niches tags

Brands are sent in batches of _BATCH_SIZE (_WORKERS batches in parallel).
Results replace the creator's previous rows in creator_brand_llm_rankings,
ranked by confidence (ties keep the nearest-first order). Brands the LLM
skipped or whose batch failed get no row.

Runs in the background after every creator-profile save (after
compute_creator_signals, so content_tags/embedding_text are fresh) and on
POST /matches/me/v2/refresh. Prompt: creator_brand_llm_ranking (editable
via /admin > Prompts).
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from pipeline.db import BrandNiche, CreatorBrandLlmRanking, CreatorProfile, Prompt
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template
from pipeline.helpers.prompts import (
    LLM_BRAND_RANKING_DEFAULT_PROMPT,
    LLM_BRAND_RANKING_PROMPT_NAME,
)
from pipeline.matching.matcher import hard_filtered_brands

logger = logging.getLogger(__name__)

_MAX_BRANDS = 300          # nearest-first cap on hard-filtered brands sent to the LLM
_BATCH_SIZE = 20           # brands per LLM call
_WORKERS = 4               # LLM calls in flight at once
_BATCH_TIMEOUT = 180.0     # seconds per LLM call (large payload)
_DESCRIPTION_LIMIT = 400   # chars of each brand description
_TAG_LIMIT = 20            # tags per brand


def _get_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == LLM_BRAND_RANKING_PROMPT_NAME).first()
    return row.content if row else LLM_BRAND_RANKING_DEFAULT_PROMPT


def _join(values) -> str:
    items = [str(v).strip() for v in (values or []) if str(v).strip()]
    return ", ".join(dict.fromkeys(items)) or "none"


def _creator_fields(creator: CreatorProfile) -> dict[str, str]:
    niches = [
        niche.strip()
        for value in (creator.instagram_primary_niche, creator.youtube_primary_niche, creator.content_niche)
        for niche in (value or "").split(",")
        if niche.strip()
    ]
    descriptions = [
        d.strip() for d in (creator.instagram_description, creator.youtube_description, creator.content_description)
        if d and d.strip()
    ]
    return {
        "creator_niches":      _join(niches),
        "creator_sub_niches":  _join([*(creator.instagram_sub_niches or []), *(creator.youtube_sub_niches or [])]),
        "content_tags":        _join(creator.content_tags),
        "creator_description": "\n".join(dict.fromkeys(descriptions)) or "none",
        "embedding_text":      (creator.embedding_text or "").strip() or "none",
    }


def _brand_payloads(db: Session, brands: list) -> list[dict]:
    ids = [brand.id for brand in brands]
    tags_by_brand: dict[int, list[str]] = {}
    niche_description: dict[int, str] = {}
    for brand_raw_id, tags, description in (
        db.query(BrandNiche.brand_raw_id, BrandNiche.tags, BrandNiche.description)
        .filter(BrandNiche.brand_raw_id.in_(ids))
        .all()
    ):
        if isinstance(tags, list):
            bucket = tags_by_brand.setdefault(brand_raw_id, [])
            bucket.extend(t.strip() for t in tags if isinstance(t, str) and t.strip() and t.strip() not in bucket)
        if description and brand_raw_id not in niche_description:
            niche_description[brand_raw_id] = description

    return [
        {
            "id":          brand.id,
            "name":        brand.name,
            "niche":       brand.niche,
            "description": ((brand.description or niche_description.get(brand.id) or "").strip()[:_DESCRIPTION_LIMIT]) or None,
            "tags":        tags_by_brand.get(brand.id, [])[:_TAG_LIMIT],
        }
        for brand in brands
    ]


def _rank_batch(template: str, creator_fields: dict, batch: list[dict], creator_id: int) -> dict[int, tuple[int, str]]:
    prompt = fill_template(template, **creator_fields, brands_json=json.dumps(batch, ensure_ascii=False))
    result = call_gpt_json(
        prompt,
        context=f"llm brand ranking creator_id={creator_id} ({len(batch)} brands)",
        timeout=_BATCH_TIMEOUT,
    )
    rankings = result.get("rankings") if isinstance(result, dict) else None
    if not isinstance(rankings, list):
        logger.warning("LLM ranking: creator_id=%d — a batch of %d brands returned no rankings", creator_id, len(batch))
        return {}

    batch_ids = {item["id"] for item in batch}
    scored: dict[int, tuple[int, str]] = {}
    for item in rankings:
        if not isinstance(item, dict):
            continue
        try:
            brand_id = int(item.get("id"))
            confidence = max(0, min(100, round(float(item.get("confidence")))))
        except (TypeError, ValueError):
            continue
        if brand_id in batch_ids:
            reason = item.get("reason")
            scored[brand_id] = (confidence, reason.strip() if isinstance(reason, str) else "")
    return scored


def rank_brands_with_llm(db: Session, creator_id: int) -> int:
    """
    Rank the creator's hard-filtered brands with the LLM and replace their
    creator_brand_llm_rankings rows. Returns the number of brands ranked.
    Does not touch llm_ranking_status — the caller (api/app.py job) owns it.
    """
    creator = db.query(CreatorProfile).filter(CreatorProfile.id == creator_id).first()
    if not creator:
        logger.warning("LLM ranking: creator_id=%d not found", creator_id)
        return 0

    brands = [brand for brand, _profile, _distance in hard_filtered_brands(db, creator, limit=_MAX_BRANDS)]
    payloads = _brand_payloads(db, brands)
    creator_fields = _creator_fields(creator)
    template = _get_prompt(db)
    db.commit()  # release read locks before the slow LLM calls

    batches = [payloads[i:i + _BATCH_SIZE] for i in range(0, len(payloads), _BATCH_SIZE)]
    logger.info("LLM ranking: creator_id=%d — %d brand(s) in %d batch(es)", creator_id, len(payloads), len(batches))

    scored: dict[int, tuple[int, str]] = {}
    if batches:
        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            for batch_result in pool.map(lambda b: _rank_batch(template, creator_fields, b, creator_id), batches):
                scored.update(batch_result)

    # Highest confidence first; ties keep the nearest-first embedding order.
    order = {payload["id"]: i for i, payload in enumerate(payloads)}
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1][0], order[kv[0]]))

    db.query(CreatorBrandLlmRanking).filter(CreatorBrandLlmRanking.creator_profile_id == creator_id).delete(
        synchronize_session=False
    )
    db.add_all(
        CreatorBrandLlmRanking(
            creator_profile_id=creator_id,
            brand_raw_id=brand_id,
            confidence=confidence,
            reason=reason or None,
            rank=rank,
        )
        for rank, (brand_id, (confidence, reason)) in enumerate(ranked, start=1)
    )
    db.commit()

    logger.info(
        "LLM ranking: creator_id=%d — ranked %d/%d brand(s)%s",
        creator_id, len(ranked), len(payloads),
        f", top: {ranked[0][0]} ({ranked[0][1][0]}%)" if ranked else "",
    )
    return len(ranked)
