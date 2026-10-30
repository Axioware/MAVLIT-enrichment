from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from api import usage_limits
from api.paging import MAX_OFFSET, SAVED_BRANDS_PAGE_DEFAULT, SAVED_BRANDS_PAGE_MAX
from api.auth import get_completed_user
from api.schemas import SavedBrandResponse, saved_brand_to_response
from pipeline.db import BrandRaw, CreatorProfile, SavedBrand, get_db

router = APIRouter(prefix="/saved-brands", tags=["saved-brands"])


class SaveBrandRequest(BaseModel):
    brand_id: int = Field(ge=1)


@router.post("", status_code=201)
def save_brand(
    body: SaveBrandRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    if not db.query(BrandRaw).filter(BrandRaw.id == body.brand_id).first():
        raise HTTPException(status_code=404, detail="Brand not found")
    already = db.query(SavedBrand).filter(SavedBrand.creator_profile_id == current_user.id, SavedBrand.brand_raw_id == body.brand_id).first()
    if already is not None:
        response.status_code = 200   # saved before: nothing new was created
        return {"brand_id": body.brand_id, "saved": True}
    maximum = usage_limits.saved_brands_max()
    if db.query(func.count()).select_from(SavedBrand).filter(SavedBrand.creator_profile_id == current_user.id).scalar() >= maximum:
        raise HTTPException(status_code=409, detail=f"You can save up to {maximum} brands. Remove one to save another.")
    stmt = (
        pg_insert(SavedBrand)
        .values(creator_profile_id=current_user.id, brand_raw_id=body.brand_id)
        .on_conflict_do_nothing(index_elements=["creator_profile_id", "brand_raw_id"])
    )
    db.execute(stmt)
    db.commit()
    return {"brand_id": body.brand_id, "saved": True}


@router.get("/me", response_model=list[SavedBrandResponse])
def list_saved_brands(
    response: Response,
    limit: int = Query(SAVED_BRANDS_PAGE_DEFAULT, ge=1, le=SAVED_BRANDS_PAGE_MAX),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    """
    The creator's saved brands, newest first, as a plain list (the same shape as before). A page of `limit` (default 100, at most 500, which is
    the most brands one user can save) starting at `offset`; the header X-Total-Count says how many are saved in all.
    """
    total = db.query(func.count()).select_from(SavedBrand).filter(SavedBrand.creator_profile_id == current_user.id).scalar()
    rows = (
        db.query(SavedBrand, BrandRaw)
        .join(BrandRaw, BrandRaw.id == SavedBrand.brand_raw_id)
        .filter(SavedBrand.creator_profile_id == current_user.id)
        .order_by(SavedBrand.created_at.desc(), SavedBrand.brand_raw_id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    response.headers["X-Total-Count"] = str(total)
    return [saved_brand_to_response(brand, saved.created_at) for saved, brand in rows]


@router.delete("/{brand_id}")
def unsave_brand(
    brand_id: int,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    row = (
        db.query(SavedBrand)
        .filter(SavedBrand.creator_profile_id == current_user.id, SavedBrand.brand_raw_id == brand_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Brand is not saved")
    db.delete(row)
    db.commit()
    return {"brand_id": brand_id, "saved": False}
