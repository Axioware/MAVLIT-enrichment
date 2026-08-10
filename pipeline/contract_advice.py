"""
pipeline/contract_advice.py

LLM-backed contract review for a creator, helping them spot common
sponsorship-contract red flags. Persisted to contract_reviews for history.
"""

import logging

from sqlalchemy.orm import Session

from pipeline.db import ContractReview, CreatorProfile, Prompt
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template
from pipeline.helpers.prompts import CONTRACT_ADVICE_DEFAULT_PROMPT, CONTRACT_ADVICE_PROMPT_NAME

logger = logging.getLogger(__name__)


def _get_contract_advice_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == CONTRACT_ADVICE_PROMPT_NAME).first()
    return row.content if row else CONTRACT_ADVICE_DEFAULT_PROMPT


def review_contract(db: Session, creator: CreatorProfile, contract_text: str) -> ContractReview:
    """Raises RuntimeError if the LLM call comes back empty — nothing is persisted in that case."""
    prompt = fill_template(_get_contract_advice_prompt(db), contract_text=contract_text)
    result = call_gpt_json(prompt, context=f"contract advice for creator_id={creator.id}")
    if not isinstance(result, dict) or not result:
        raise RuntimeError("Contract review failed — LLM returned no content")

    issues = result.get("issues")
    issues = [i for i in issues if isinstance(i, str)] if isinstance(issues, list) else []

    review = ContractReview(
        creator_profile_id=creator.id,
        contract_text=contract_text,
        looks_good=result.get("looks_good"),
        issues=issues,
        summary=result.get("summary"),
    )
    db.add(review)
    db.commit()
    db.refresh(review)
    logger.info("Contract review: creator_id=%d id=%d looks_good=%s", creator.id, review.id, review.looks_good)
    return review
