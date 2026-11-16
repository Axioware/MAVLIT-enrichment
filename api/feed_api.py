"""GET /feed/similar-creators: the creators shown in the Feed's "Similar Creators" tab (real data, pipeline/similar_creators.py)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from api.auth import get_current_user
from pipeline.db import CreatorProfile, get_db
from pipeline.helpers.gpt_llm import LLMUnavailableError
from pipeline.similar_creators import MIN_PARTNERSHIP_CONFIDENCE, SIMILARITY_THRESHOLD, find_similar_creators, user_query_embedding, user_query_text

router = APIRouter(prefix="/feed", tags=["feed"])


@router.get("/similar-creators")
def similar_creators(db: Session = Depends(get_db), user: CreatorProfile = Depends(get_current_user)):
    """
    Creators with a confirmed partnership (score >= 90) whose profile is at least 70 % similar to the signed-in creator's own profile (compared in the creators' own Niche / Description / Tags format).
    {"ready": false} when the user's profile has no niche and no description yet, so the page can say so instead of showing nothing.
    """
    base = {"threshold": round(SIMILARITY_THRESHOLD * 100), "min_partnership_score": MIN_PARTNERSHIP_CONFIDENCE}
    if user_query_text(user) is None:   # no niche and no description yet: nothing to compare
        return {**base, "ready": False, "creators": []}
    try:
        vector = user_query_embedding(user)
    except LLMUnavailableError:
        raise HTTPException(status_code=503, detail="Similar creators are not available right now. Please try again in a few minutes.")
    if vector is None:
        raise HTTPException(status_code=503, detail="Similar creators are not available right now. Please try again in a few minutes.")
    return {**base, "ready": True, "creators": find_similar_creators(db, vector, own_handle=user.instagram_handle)}
