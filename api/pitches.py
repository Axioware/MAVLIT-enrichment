from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.auth import get_current_user
from api.schemas import PitchRequest, PitchResponse, pitch_to_response
from pipeline.db import CreatorProfile, Pitch, get_db
from pipeline.pitching import generate_pitch

router = APIRouter(prefix="/pitches", tags=["pitches"])


@router.post("", response_model=PitchResponse)
def create_pitch(
    body: PitchRequest,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_current_user),
):
    if body.is_custom:
        if not body.custom_brand_name or body.brand_id is not None:
            raise HTTPException(status_code=400, detail="custom_brand_name is required (and brand_id must be omitted) for a custom pitch")
    elif body.brand_id is None:
        raise HTTPException(status_code=400, detail="brand_id is required for a non-custom pitch")

    try:
        pitch = generate_pitch(
            db, current_user,
            is_custom=body.is_custom,
            brand_id=body.brand_id,
            custom_brand_name=body.custom_brand_name,
            story=body.story,
            product_reference=body.product_reference,
            past_brand_partnership=body.past_brand_partnership,
            content_link=body.content_link,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return pitch_to_response(pitch)


@router.get("/me", response_model=list[PitchResponse])
def list_pitches(
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_current_user),
):
    rows = (
        db.query(Pitch)
        .filter(Pitch.creator_profile_id == current_user.id)
        .order_by(Pitch.created_at.desc())
        .all()
    )
    return [pitch_to_response(p) for p in rows]
