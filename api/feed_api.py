"""GET /feed/similar-creators: the creators shown in the Feed's "Similar Creators" tab (real data, pipeline/similar_creators.py)."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from api.auth import get_current_user
from pipeline.db import CreatorProfile, get_db
from pipeline.helpers.gpt_llm import LLMUnavailableError
from pipeline.similar_creators import MIN_PARTNERSHIP_CONFIDENCE, SIMILARITY_THRESHOLD, audience_range, find_similar_creators, user_query_embedding, user_query_text

router = APIRouter(prefix="/feed", tags=["feed"])


@router.get("/similar-creators")
def similar_creators(same_audience: bool = Query(False), db: Session = Depends(get_db), user: CreatorProfile = Depends(get_current_user)):
    """
    Creators with a confirmed partnership (score >= 90) whose profile is at least 70 % similar to the signed-in creator's own profile (compared in the creators' own Niche / Description / Tags format).
    ?same_audience=true is a hard filter on the follower count: only creators with between a third of the user's followers and twice as many.
    {"ready": false} when the user's profile has no niche and no description yet, so the page can say so instead of showing nothing.
    """
    followers = user.instagram_followers or user.follower_count
    span = audience_range(followers)
    base = {"threshold": round(SIMILARITY_THRESHOLD * 100), "min_partnership_score": MIN_PARTNERSHIP_CONFIDENCE,
            "your_followers": followers, "audience_range": {"min": span[0], "max": span[1]} if span else None}
    if user_query_text(user) is None:   # no niche and no description yet: nothing to compare
        return {**base, "ready": False, "creators": []}
    if same_audience and span is None:   # the filter needs the user's follower count: nothing to show, and no embedding call
        return {**base, "ready": True, "creators": []}
    try:
        vector = user_query_embedding(user)
    except LLMUnavailableError:
        raise HTTPException(status_code=503, detail="Similar creators are not available right now. Please try again in a few minutes.")
    if vector is None:
        raise HTTPException(status_code=503, detail="Similar creators are not available right now. Please try again in a few minutes.")
    return {**base, "ready": True,
            "creators": find_similar_creators(db, vector, own_handle=user.instagram_handle, followers_range=span if same_audience else None)}
