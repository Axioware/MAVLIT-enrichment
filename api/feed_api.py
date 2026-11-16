"""GET /feed/similar-creators: the creators shown in the Feed's "Similar Creators" tab (real data, pipeline/similar_creators.py)."""
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from api.auth import get_current_user
from api.creator_posts import ThumbnailTooLarge, _fetch_thumbnail
from pipeline.db import BrandRaw, CreatorProfile, get_db
from pipeline.brand_intelligence import brand_creators, brand_stats
from pipeline.helpers.gpt_llm import LLMUnavailableError
from pipeline.similar_creators import MIN_PARTNERSHIP_CONFIDENCE, SIMILARITY_THRESHOLD, audience_range, find_similar_creators, is_partnership_post_code, your_stats, user_query_embedding, user_query_text

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
            "your_followers": followers, "audience_range": {"min": span[0], "max": span[1]} if span else None,
            "you": your_stats(db, user)}   # the user's own numbers, for "Why Them, Not Me?"
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


_CODE = re.compile(r"^[A-Za-z0-9_-]{5,40}$")


@router.get("/post-thumbnail")
def post_thumbnail(code: str = Query(...), db: Session = Depends(get_db), user: CreatorProfile = Depends(get_current_user)):
    """
    The picture of a partnership post shown in the Similar Creators dialog (404 when there is none: private, deleted or Instagram not answering).
    Only the posts of confirmed partnerships can be asked for, and the server fetches the picture itself: the browser cannot load it straight
    from Instagram (see api/creator_posts.py, whose cache and size limit this shares).
    """
    if not _CODE.match(code) or not is_partnership_post_code(db, code):
        raise HTTPException(status_code=404, detail="No picture for this post")
    try:
        found = _fetch_thumbnail(code)
    except ThumbnailTooLarge:
        raise HTTPException(status_code=502, detail="This post's picture is too large to show")
    if found is None:
        raise HTTPException(status_code=404, detail="No picture for this post")
    content, kind = found
    return Response(content=content, media_type=kind, headers={"Cache-Control": "private, max-age=3600"})


@router.get("/brand-intelligence")
def brand_intelligence(limit: int = Query(30, ge=1, le=100), db: Session = Depends(get_db), user: CreatorProfile = Depends(get_current_user)):
    """
    The Feed's Brand Intelligence tab: the brands the signed-in creator is matched with (the same list as the Brand Matches page, Matches v3, best
    first) with what their partnership posts (scored 90 or more) say: how many, the size of the creators, the formats and the creators' engagement.
    """
    from api.app import _ai_matches_response   # the Brand Matches page's own loader, so the two lists can never differ (api.app imports this module)
    ranked = _ai_matches_response(db, user.id, "v3")
    matches = ranked.matches[:limit]
    stats = brand_stats(db, [m.brand_raw_id for m in matches])
    return {
        "status": ranked.status,
        "total": ranked.total,
        "brands": [{
            "brand_raw_id": m.brand_raw_id, "brand_name": m.brand_name, "niche": m.niche, "website": m.website,
            "rank": m.rank, "confidence": m.confidence, **stats[m.brand_raw_id],
        } for m in matches],
    }


@router.get("/brand-intelligence/{brand_id}/creators")
def brand_intelligence_creators(brand_id: int, db: Session = Depends(get_db), user: CreatorProfile = Depends(get_current_user)):
    """The unique creators a brand works with (the creators behind its partnership posts scored 90 or more), for the "View insights" window."""
    brand = db.get(BrandRaw, brand_id)
    if brand is None:
        raise HTTPException(status_code=404, detail="Brand not found")
    return {"brand_raw_id": brand.id, "brand_name": brand.name or f"brand {brand.id}", "creators": brand_creators(db, brand.id)}
