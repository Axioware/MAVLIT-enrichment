from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.auth import get_current_user
from api.schemas import BrandDetailResponse, brand_detail_to_response
from pipeline.brand_detail import get_brand_detail
from pipeline.db import CreatorProfile, get_db

router = APIRouter(prefix="/brands", tags=["brands"])


@router.get("/{brand_id}", response_model=BrandDetailResponse)
def get_brand(
    brand_id: int,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_current_user),
):
    detail = get_brand_detail(db, brand_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Brand not found")
    return brand_detail_to_response(detail)
