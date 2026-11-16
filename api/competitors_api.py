"""
/feed/competitors: the creators a user added as competitors to compare themselves with (Feed > Similar Creators > Compare Creators).
The work is in pipeline/competitor_creators.py (an account MAVLIT already holds in instagram_users is worked out from those rows; any other gets its
latest 20 posts read with Apify). Same rules as the own-brand lookup: one active lookup per user, a daily cap, a bounded job queue.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api import usage_limits
from api.auth import get_completed_user
from api.jobs import QueueFull, busy_error, ensure_room, submit_user_job
from pipeline.competitor_creators import find_competitor, needs_job, refresh_stale, run_competitor_job, start_competitor
from pipeline.db import CompetitorCreator, CreatorProfile, UserCompetitor, get_db
from pipeline.own_brand import parse_instagram_handle

router = APIRouter(prefix="/feed/competitors", tags=["feed"])

LOCK = "competitor"
CAP = "competitors"


class CompetitorRequest(BaseModel):
    instagram: str = Field(max_length=200)   # the competitor's Instagram URL or handle


def _to_response(comp: CompetitorCreator) -> dict:
    return {
        "id": comp.id, "handle": comp.instagram_handle, "status": comp.status, "stage": comp.stage, "error": comp.error, "source": comp.source,
        "full_name": comp.full_name, "followers": comp.followers_count, "posts_analyzed": comp.posts_analyzed,
        "avg_likes": comp.avg_likes, "avg_comments": comp.avg_comments, "engagement_rate": comp.engagement_rate,
        "avg_reel_views": comp.avg_reel_views, "posts_per_week": comp.posts_per_week,
        "product_focused_pct": comp.product_focused_pct, "content_categories": comp.content_categories,
        "partnership_count": comp.partnership_count or 0, "partnership_brands": comp.partnership_brands or [],
    }


@router.get("")
def list_competitors(db: Session = Depends(get_db), user: CreatorProfile = Depends(get_completed_user)):
    """The user's competitors, the newest first, each with its status (queued / running / ready / failed) and, once ready, its numbers."""
    rows = (db.query(CompetitorCreator).join(UserCompetitor, UserCompetitor.competitor_creator_id == CompetitorCreator.id)
            .filter(UserCompetitor.creator_profile_id == user.id).order_by(UserCompetitor.created_at.desc(), CompetitorCreator.id.desc()).all())
    return {"competitors": [_to_response(refresh_stale(db, c)) for c in rows]}


@router.post("")
def add_competitor(body: CompetitorRequest, db: Session = Depends(get_db), user: CreatorProfile = Depends(get_completed_user)):
    """
    Add a competitor by Instagram handle or link. The handle is checked first: a competitor MAVLIT already has (added by anyone) is reused with no
    new work. Otherwise a lookup runs in the background (poll GET /feed/competitors until its status is ready or it is gone: no posts were found).
    """
    try:
        handle = parse_instagram_handle(body.instagram)   # a typo is a 400 and does not use up the daily cap
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    runs = needs_job(find_competitor(db, handle))
    if not runs:   # already known: just link it to this user
        try:
            comp, _ = start_competitor(db, user.id, handle)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _to_response(comp)
    ensure_room()   # 503 when the job queue is full: nothing is locked or counted yet
    bind = db.get_bind()
    if not usage_limits.try_acquire_lock(db, user.id, LOCK, usage_limits.job_lock_stale_seconds()):
        raise HTTPException(status_code=409, detail="A competitor lookup is already running. Wait for it to finish, then add the next one.")
    queued = False
    try:
        usage_limits.consume(db, user.id, CAP)   # 429 over the daily cap, before the Apify run is queued
        try:
            comp, runs = start_competitor(db, user.id, handle)
        except ValueError as exc:
            usage_limits.refund(db, user.id, CAP)
            raise HTTPException(status_code=400, detail=str(exc))
        if runs:
            try:
                submit_user_job(run_competitor_job, comp.id, user.id, creator_id=user.id, locks=(LOCK,), bind=bind)
                queued = True
            except QueueFull:   # lost the race for the last place in the queue: not run, not counted
                comp.status, comp.stage, comp.error = "failed", "Stopped", "The server is busy, please try again in a moment"
                db.commit()
                usage_limits.refund(db, user.id, CAP)
                raise busy_error()
        else:
            usage_limits.refund(db, user.id, CAP)   # someone else's lookup made it ready meanwhile
        return _to_response(comp)
    finally:
        if not queued:
            usage_limits.release_lock(bind, user.id, LOCK)


@router.delete("/{competitor_id}")
def remove_competitor(competitor_id: int, db: Session = Depends(get_db), user: CreatorProfile = Depends(get_completed_user)):
    """Take a competitor off the user's list (its numbers stay for anyone else who added it)."""
    link = db.get(UserCompetitor, (user.id, competitor_id))
    if link is None:
        raise HTTPException(status_code=404, detail="Competitor not found")
    db.delete(link)
    db.commit()
    return {"removed": competitor_id}
