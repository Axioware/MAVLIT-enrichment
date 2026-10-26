"""
pipeline/contract_advice.py

LLM-backed contract review for a creator, helping them spot common
sponsorship-contract red flags. Persisted to contract_reviews for history.
"""

import logging

from sqlalchemy.orm import Session

from pipeline.db import ContractReview, CreatorProfile, Prompt
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template, llm_usage_scope
from pipeline.helpers.prompts import (
    CONTRACT_ADVICE_DEFAULT_PROMPT,
    CONTRACT_ADVICE_PROMPT_NAME,
)

logger = logging.getLogger(__name__)


def _get_contract_advice_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == CONTRACT_ADVICE_PROMPT_NAME).first()
    return row.content if row else CONTRACT_ADVICE_DEFAULT_PROMPT


def _deal_values(brand_name, deliverable_type, exclusivity, usage, duration_months) -> dict[str, str]:
    """The Deal Tools terms as prompt text; "not set" for any the creator left empty."""
    from pipeline.rate_intelligence import EXCLUSIVITY_TEXT   # lazy: that module pulls in the pitch/matching code

    exclusivity_text = EXCLUSIVITY_TEXT.get(exclusivity or "", "not set")
    if exclusivity in ("Narrow", "Broad"):
        exclusivity_text += f", for {duration_months} months" if duration_months else ", duration not set"
    return {
        "brand_name": (brand_name or "").strip() or "not set",
        "deliverable_type": f"{deliverable_type.strip()} (the kind of content; how many is not set)" if (deliverable_type or "").strip() else "not set",
        "exclusivity": exclusivity_text,
        "usage": (usage or "").strip() or "not set",
    }


def review_contract(
    db: Session,
    creator: CreatorProfile,
    contract_text: str,
    *,
    brand_name: str | None = None,
    deliverable_type: str | None = None,
    exclusivity: str | None = None,
    usage: str | None = None,
    duration_months: int | None = None,
) -> ContractReview:
    """
    Raises RuntimeError if the LLM call comes back empty - nothing is persisted in that case. The optional Deal Tools
    terms are what the contract is compared with ("this contract's exclusivity is broader than the Narrow term you chose").
    """
    values = _deal_values(brand_name, deliverable_type, exclusivity, usage, duration_months)
    prompt = fill_template(_get_contract_advice_prompt(db), contract_text=contract_text, **values)
    with llm_usage_scope(creator.id, "contract_advice"):
        result = call_gpt_json(prompt, context=f"contract advice for creator_id={creator.id}")
    if not isinstance(result, dict) or not result:
        raise RuntimeError("Contract review failed - LLM returned no content")

    def texts(key: str) -> list[str]:
        value = result.get(key)
        return [i.strip() for i in value if isinstance(i, str) and i.strip()] if isinstance(value, list) else []

    terms_set = {k: v for k, v in {
        "deliverable_type": deliverable_type, "exclusivity": exclusivity, "usage": usage,
        "duration_months": duration_months if exclusivity in ("Narrow", "Broad") else None,
    }.items() if v}
    review = ContractReview(
        creator_profile_id=creator.id,
        contract_text=contract_text,
        looks_good=result.get("looks_good") if isinstance(result.get("looks_good"), bool) else None,
        issues=texts("issues"),
        summary=result.get("summary"),
        brand_name=(brand_name or "").strip() or None,
        deal_context=terms_set,
        deal_comparison=texts("deal_comparison") if terms_set else [],
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    logger.info("Contract review: creator_id=%d id=%d looks_good=%s", creator.id, review.id, review.looks_good)
    return review
