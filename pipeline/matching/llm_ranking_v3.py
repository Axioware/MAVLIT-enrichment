"""
pipeline/matching/llm_ranking_v3.py

Matches v3 — a mix of v1 and v2. Like v2 (llm_ranking_v2.py), the LLM ranks
every brand passing the Stage 3 hard filters against the creator's
profile; v3 additionally gives it each brand's "why it's a match"
taglines (match_text.generate_match_reasons — the evidence v1 shows). The
prompt has the LLM score content fit and that match evidence separately and
return confidence = 0.65 x content fit + 0.35 x match evidence.

  creator — niche(s), sub-niche tags, content_tags, description(s),
            embedding_text                     (same as v2)
  brand   — name, niche, description, brands_niches tags, why_it_matches

Batching, model (gpt-5), parsing and the fully-replaced results table
mirror v2; results go to creator_brand_llm_v3_rankings with the taglines
stored alongside. Runs in the background after every creator-profile save
(after v2) and on POST /matches/me/v3/refresh. Prompt:
creator_brand_llm_ranking_v3 (editable via /admin > Prompts).
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from pipeline.db import CreatorBrandLlmV3Ranking, CreatorProfile, Prompt
from pipeline.helpers.prompts import (
    LLM_BRAND_RANKING_V3_DEFAULT_PROMPT,
    LLM_BRAND_RANKING_V3_PROMPT_NAME,
)
from pipeline.matching.llm_ranking_v2 import (
    _BATCH_SIZE,
    _MAX_BRANDS,
    _WORKERS,
    _brand_payloads,
    _creator_fields,
    _rank_batch,
)
from pipeline.matching.match_text import generate_match_reasons
from pipeline.matching.matcher import hard_filtered_brands

logger = logging.getLogger(__name__)

_MODEL = "gpt-5"


def _get_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == LLM_BRAND_RANKING_V3_PROMPT_NAME).first()
    return row.content if row else LLM_BRAND_RANKING_V3_DEFAULT_PROMPT


def rank_brands_with_llm_v3(db: Session, creator_id: int) -> int:
    """
    Rank the creator's hard-filtered brands with the LLM using profile fit
    plus each brand's taglines, and replace their creator_brand_llm_v3_rankings
    rows. Returns the number of brands ranked. Does not touch
    llm_v3_ranking_status — the caller (api/app.py job) owns it.
    """
    creator = db.query(CreatorProfile).filter(CreatorProfile.id == creator_id).first()
    if not creator:
        logger.warning("LLM ranking v3: creator_id=%d not found", creator_id)
        return 0

    candidates = hard_filtered_brands(db, creator, limit=_MAX_BRANDS)
    taglines = {
        brand.id: generate_match_reasons(creator, brand, profile, db=db)
        for brand, profile, _distance in candidates
    }
    payloads = _brand_payloads(db, [brand for brand, _profile, _distance in candidates])
    for payload in payloads:
        payload["why_it_matches"] = taglines[payload["id"]]
    creator_fields = _creator_fields(creator)
    template = _get_prompt(db)
    db.commit()  # release read locks before the slow LLM calls

    batches = [payloads[i:i + _BATCH_SIZE] for i in range(0, len(payloads), _BATCH_SIZE)]
    logger.info("LLM ranking v3: creator_id=%d — %d brand(s) in %d batch(es)", creator_id, len(payloads), len(batches))

    scored: dict[int, tuple[int, str]] = {}
    if batches:
        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            for batch_result in pool.map(
                lambda b: _rank_batch(template, creator_fields, b, creator_id, model=_MODEL), batches,
            ):
                scored.update(batch_result)

    # Highest confidence first; ties keep the nearest-first embedding order.
    order = {payload["id"]: i for i, payload in enumerate(payloads)}
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1][0], order[kv[0]]))

    db.query(CreatorBrandLlmV3Ranking).filter(CreatorBrandLlmV3Ranking.creator_profile_id == creator_id).delete(
        synchronize_session=False
    )
    db.add_all(
        CreatorBrandLlmV3Ranking(
            creator_profile_id=creator_id,
            brand_raw_id=brand_id,
            confidence=confidence,
            reason=reason or None,
            reasons=taglines[brand_id],
            rank=rank,
        )
        for rank, (brand_id, (confidence, reason)) in enumerate(ranked, start=1)
    )
    db.commit()

    logger.info(
        "LLM ranking v3: creator_id=%d — ranked %d/%d brand(s)%s",
        creator_id, len(ranked), len(payloads),
        f", top: {ranked[0][0]} ({ranked[0][1][0]}%)" if ranked else "",
    )
    return len(ranked)
