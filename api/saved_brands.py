from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from api.auth import get_current_user
from api.schemas import SavedBrandResponse, saved_brand_to_response
from pipeline.db import BrandRaw, CreatorProfile, SavedBrand, get_db

router = APIRouter(prefix="/saved-brands", tags=["saved-brands"])


class SaveBrandRequest(BaseModel):
    brand_id: int


@router.post("", status_code=201)
def save_brand(
    body: SaveBrandRequest,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_current_user),
):
    if not db.query(BrandRaw).filter(BrandRaw.id == body.brand_id).first():
        raise HTTPException(status_code=404, detail="Brand not found")
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
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_current_user),
):
    rows = (
        db.query(SavedBrand, BrandRaw)
        .join(BrandRaw, BrandRaw.id == SavedBrand.brand_raw_id)
        .filter(SavedBrand.creator_profile_id == current_user.id)
        .order_by(SavedBrand.created_at.desc())
        .all()
    )
    return [saved_brand_to_response(brand, saved.created_at) for saved, brand in rows]


@router.delete("/{brand_id}")
def unsave_brand(
    brand_id: int,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_current_user),
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
