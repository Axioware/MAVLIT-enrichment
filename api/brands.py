from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.auth import get_completed_user
from api.contact_access import allow_contacts
from api.schemas import BrandDetailResponse, brand_detail_to_response
from pipeline.brand_detail import get_brand_detail
from pipeline.db import CreatorProfile, get_db

router = APIRouter(prefix="/brands", tags=["brands"])


@router.get("/{brand_id}", response_model=BrandDetailResponse)
def get_brand(
    brand_id: int,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    detail = get_brand_detail(db, brand_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Brand not found")
    # The brand's contact people only for a brand the creator saved, is matched to or added themselves, and within the daily cap
    # (429 over it). For any other brand they are left out, not an error.
    if (detail["top_contact"] or detail["other_contacts"]) and not allow_contacts(db, current_user.id, brand_id):
        detail = {**detail, "top_contact": None, "other_contacts": []}
    return brand_detail_to_response(detail)
