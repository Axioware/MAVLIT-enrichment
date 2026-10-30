import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from api import usage_limits
from api.auth import get_completed_user
from api.schemas import (
    ContractAdviceRequest,
    ContractAdviceResponse,
    RateIntelligenceRequest,
    RateIntelligenceResponse,
    contract_review_to_response,
    rate_estimate_to_response,
)
from pipeline.contract_advice import review_contract
from pipeline.db import CreatorProfile, RateEstimate, get_db
from pipeline.rate_intelligence import estimate_rate

logger = logging.getLogger(__name__)
router = APIRouter(tags=["advisory"])

RATE_UNAVAILABLE = "Rate estimation is temporarily unavailable, try again"   # shown instead of any provider / internal error text
CONTRACT_UNAVAILABLE = "Contract review is temporarily unavailable, try again"


@router.post("/rate-intelligence", response_model=RateIntelligenceResponse)
def get_rate_intelligence(
    body: RateIntelligenceRequest,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    if body.brand_id is None and not (body.brand_name or "").strip():
        raise HTTPException(status_code=422, detail="brand_id or brand_name is required")
    usage_limits.consume(db, current_user.id, "rate_intelligence")   # 429 over the daily cap, before any LLM call
    try:
        estimate = estimate_rate(
            db, current_user,
            brand_id=body.brand_id,
            brand_name=body.brand_name,
            platform=body.platform,
            deliverable_type=body.deliverable_type,
            exclusivity=body.exclusivity,
            usage=body.usage,
            duration_months=body.duration_months,
        )
    except ValueError as exc:   # a brand_id that does not exist
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        logger.warning("Rate estimation failed for creator_id=%s: %s", current_user.id, exc)
        raise HTTPException(status_code=502, detail=RATE_UNAVAILABLE)
    return rate_estimate_to_response(estimate)


@router.get("/rate-intelligence/latest", response_model=RateIntelligenceResponse | None)
def get_latest_rate_intelligence(
    brand_id: int | None = Query(None, ge=1),
    brand_name: str | None = Query(None, max_length=200),
    deliverable_type: str | None = Query(None, max_length=100),
    exclusivity: str | None = Query(None, max_length=100),
    usage: str | None = Query(None, max_length=100),
    duration_months: int | None = Query(None, ge=1, le=120),
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    """
    The creator's saved estimate, so it is still there after the page is reloaded (null when there is none).
      - with a brand and a deliverable type: the newest estimate made for exactly those inputs (the months only
        count for Narrow / Broad exclusivity);
      - with no inputs: the creator's newest estimate of all, whose inputs the page restores.
    """
    query = db.query(RateEstimate).filter(RateEstimate.creator_profile_id == current_user.id, RateEstimate.status == "ok")
    if deliverable_type and (brand_id is not None or (brand_name or "").strip()):
        if brand_id is not None:
            query = query.filter(RateEstimate.brand_raw_id == brand_id)
        else:
            query = query.filter(func.lower(RateEstimate.brand_name) == brand_name.strip().lower())
        query = query.filter(RateEstimate.deliverable_type == deliverable_type)
        query = query.filter(RateEstimate.exclusivity.is_(None) if not exclusivity else RateEstimate.exclusivity == exclusivity)
        query = query.filter(RateEstimate.usage.is_(None) if not usage else RateEstimate.usage == usage)
        if exclusivity in ("Narrow", "Broad"):
            query = query.filter(RateEstimate.duration_months.is_(None) if duration_months is None else RateEstimate.duration_months == duration_months)
    estimate = query.order_by(RateEstimate.created_at.desc(), RateEstimate.id.desc()).first()
    return rate_estimate_to_response(estimate) if estimate else None


@router.post("/contract-advice", response_model=ContractAdviceResponse)
def get_contract_advice(
    body: ContractAdviceRequest,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    usage_limits.consume(db, current_user.id, "contract_advice")   # 429 over the daily cap, before any LLM call
    try:
        review = review_contract(
            db, current_user, body.contract_text,
            brand_name=body.brand_name, deliverable_type=body.deliverable_type, exclusivity=body.exclusivity,
            usage=body.usage, duration_months=body.duration_months,
        )
    except RuntimeError as exc:
        logger.warning("Contract review failed for creator_id=%s: %s", current_user.id, exc)
        raise HTTPException(status_code=502, detail=CONTRACT_UNAVAILABLE)
    return contract_review_to_response(review)
