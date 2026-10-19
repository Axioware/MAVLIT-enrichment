"""
pipeline/matching/llm_ranking_v3.py

Matches v3 — a mix of v1 and v2. Like v2 (llm_ranking_v2.py), the LLM ranks
every brand passing the Stage 3 hard filters against the creator's
profile; v3 additionally gives it each brand's "why it's a match"
taglines (match_text.generate_match_reasons — the evidence v1 shows). The
prompt has the LLM score content fit and that match evidence separately and
return confidence = 0.65 x content fit + 0.35 x match evidence.

  creator — niche(s), sub-niche tags, content_tags, description(s),
            embedding_text (same as v2), gender, and the creator's own
            free-text brand preferences (creator_profiles.brand_preferences:
            brand types they want to see more of / do not want) — wanted
            brands are ranked higher, unwanted ones last
  brand   — name, niche, description, brands_niches tags, why_it_matches,
            target_audience_gender, product_audience_gender

Unlike v1/v2, v3 does NOT apply the matcher's gender hard filter: brands of
every target audience gender (male / female / both) are sent, and the
prompt has the LLM weigh gender fit itself as a soft signal (a
female-targeted brand whose products or partnerships also suit men is not
excluded for a male creator). Brands with NO target_audience_gender
(NULL) are sent too, as "unknown", which the prompt treats as neutral.

Beach brands: every brand carries "beach_brand" (brands_raw.beach_brands),
and the prompt gets a BEACH BRANDS section (LLM_BRAND_RANKING_V3_BEACH_NOTE)
telling the model such brands sell beach / swimming / water-sport / ocean
products, so it ranks them higher for beach-related creators.

Batching, parsing and the fully-replaced results table
mirror v2; results go to creator_brand_llm_v3_rankings with the taglines
stored alongside. Runs in the background after every creator-profile save
(after v2) and on POST /matches/me/v3/refresh. Prompt:
creator_brand_llm_ranking_v3 (editable via /admin > Prompts).
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from pipeline.db import CreatorBrandLlmV3Ranking, CreatorProfile, Prompt
from pipeline.helpers.creator_gender import creator_match_gender
from pipeline.helpers.prompts import (
    LLM_BRAND_RANKING_V3_BEACH_NOTE,
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

_MODEL = "gpt-5-mini"


def _gender_label(value: str | None) -> str:
    """Brand audience gender for the prompt: male / female / both, else unknown."""
    v = (value or "").strip().lower()
    return v if v in ("male", "female", "both") else "unknown"


def _creator_gender_label(value: str | None) -> str:
    """Creator gender for the prompt: male / female, else not specified."""
    v = (value or "").strip().lower()
    return v if v in ("male", "female") else "not specified"


def _get_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == LLM_BRAND_RANKING_V3_PROMPT_NAME).first()
    return _with_beach_note(row.content if row else LLM_BRAND_RANKING_V3_DEFAULT_PROMPT)


def _with_beach_note(template: str) -> str:
    """
    Add the BEACH BRANDS guidance to the prompt (default or database copy)
    before its final JSON-format instruction — unless the prompt already has
    a BEACH BRANDS section (e.g. it was pasted into /admin). The flag itself
    reaches the model as "beach_brand" on every brand in the list.
    """
    if "BEACH BRANDS" in template:
        return template
    marker = "Reply ONLY with this JSON object"
    if marker in template:
        return template.replace(marker, LLM_BRAND_RANKING_V3_BEACH_NOTE + "\n" + marker, 1)
    return template + "\n\n" + LLM_BRAND_RANKING_V3_BEACH_NOTE


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

    # Every hard filter except the creator-vs-brand gender match — the LLM
    # judges gender fit softly, including brands with NO
    # target_audience_gender (sent as "unknown").
    candidates = hard_filtered_brands(db, creator, limit=_MAX_BRANDS, apply_gender_filter=False)
    audience = {
        brand.id: (_gender_label(brand.target_audience_gender), _gender_label(brand.product_audience_gender))
        for brand, _profile, _distance in candidates
    }
    taglines = {
        brand.id: generate_match_reasons(creator, brand, profile, db=db)
        for brand, profile, _distance in candidates
    }
    payloads = _brand_payloads(db, [brand for brand, _profile, _distance in candidates])
    beach_flag = {brand.id: bool(brand.beach_brands) for brand, _profile, _distance in candidates}
    for payload in payloads:
        payload["why_it_matches"] = taglines[payload["id"]]
        payload["target_audience_gender"], payload["product_audience_gender"] = audience[payload["id"]]
        payload["beach_brand"] = beach_flag[payload["id"]]
    creator_fields = {
        **_creator_fields(creator),
        "creator_gender": _creator_gender_label(creator_match_gender(creator)),
        # What the creator typed in the profile's "Brand preferences" box:
        # brand types to rank higher / never show (prompt: CREATOR PREFERENCE RULES).
        "creator_preferences": (creator.brand_preferences or "").strip() or "none",
    }
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
