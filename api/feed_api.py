"""GET /feed/similar-creators: the creators shown in the Feed's "Similar Creators" tab (real data, pipeline/similar_creators.py)."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.auth import get_current_user
from pipeline.db import CreatorProfile, get_db
from pipeline.similar_creators import MIN_PARTNERSHIP_CONFIDENCE, SIMILARITY_THRESHOLD, find_similar_creators

router = APIRouter(prefix="/feed", tags=["feed"])


@router.get("/similar-creators")
def similar_creators(db: Session = Depends(get_db), user: CreatorProfile = Depends(get_current_user)):
    """
    Creators with a confirmed partnership (score >= 90) whose profile is at least 70 % similar to the signed-in creator's own profile.
    {"ready": false} when the user's profile has no embedding yet (the profile is not complete), so the page can say so instead of showing nothing.
    """
    base = {"threshold": round(SIMILARITY_THRESHOLD * 100), "min_partnership_score": MIN_PARTNERSHIP_CONFIDENCE}
    if user.embedding is None:
        return {**base, "ready": False, "creators": []}
    return {**base, "ready": True, "creators": find_similar_creators(db, user.embedding, own_handle=user.instagram_handle)}
