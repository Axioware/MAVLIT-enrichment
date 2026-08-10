from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.auth import get_current_user
from api.schemas import (
    ContractAdviceRequest, ContractAdviceResponse, contract_review_to_response,
    RateIntelligenceRequest, RateIntelligenceResponse, rate_estimate_to_response,
)
from pipeline.contract_advice import review_contract
from pipeline.db import CreatorProfile, get_db
from pipeline.rate_intelligence import estimate_rate

router = APIRouter(tags=["advisory"])


@router.post("/rate-intelligence", response_model=RateIntelligenceResponse)
def get_rate_intelligence(
    body: RateIntelligenceRequest,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_current_user),
):
    try:
        estimate = estimate_rate(
            db, current_user,
            brand_id=body.brand_id,
            platform=body.platform,
            deliverable_type=body.deliverable_type,
            exclusivity=body.exclusivity,
            usage=body.usage,
            duration_months=body.duration_months,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return rate_estimate_to_response(estimate)


@router.post("/contract-advice", response_model=ContractAdviceResponse)
def get_contract_advice(
    body: ContractAdviceRequest,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_current_user),
):
    try:
        review = review_contract(db, current_user, body.contract_text)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return contract_review_to_response(review)
