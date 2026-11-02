import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api import usage_limits
from api.jobs import QueueFull, busy_error, ensure_room, submit_user_job
from api.auth import get_completed_user
from pipeline.db import BrandRaw, CreatorProfile, OwnBrandJob, get_db
from pipeline.own_brand import parse_instagram_handle, refresh_stale, run_own_brand_job, start_own_brand_job

router = APIRouter(prefix="/own-brands", tags=["own-brands"])


class OwnBrandRequest(BaseModel):
    instagram: str = Field(max_length=200)   # the brand's Instagram URL or handle


class OwnBrandInfo(BaseModel):
    brand_id: int
    name: str | None = None
    instagram_handle: str
    niche: str | None = None
    description: str | None = None
    target_audience_gender: str | None = None
    target_audience_min_age: int | None = None    # an estimate, not confirmed
    target_audience_max_age: int | None = None
    latest_product: str | None = None


class OwnBrandJobResponse(BaseModel):
    job_id: str
    status: str                  # queued | running | ready | failed
    stage: str | None = None     # what MAVLIT is doing right now, for the page to show
    error: str | None = None
    brand: OwnBrandInfo | None = None   # filled in once status is "ready"


def _to_response(db: Session, job: OwnBrandJob) -> OwnBrandJobResponse:
    brand = db.get(BrandRaw, job.brand_raw_id) if job.brand_raw_id else None
    info = None
    if brand is not None and job.status == "ready":
        info = OwnBrandInfo(
            brand_id=brand.id,
            name=brand.name,
            instagram_handle=job.instagram_handle,
            niche=brand.niche,
            description=brand.description,
            target_audience_gender=brand.target_audience_gender,
            target_audience_min_age=brand.target_audience_min_age,
            target_audience_max_age=brand.target_audience_max_age,
            latest_product=brand.latest_product,
        )
    return OwnBrandJobResponse(job_id=job.id, status=job.status, stage=job.stage, error=job.error, brand=info)


@router.post("", response_model=OwnBrandJobResponse)
def add_own_brand(
    body: OwnBrandRequest,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    """
    "My own brand" on the pitch page. Adds the brand's Instagram account to MAVLIT (or reuses it if
    it is already there), then in the background reads its latest 40 posts and writes the brand profile
    (name, description, niche, target audience, latest product). Poll GET /own-brands/jobs/{job_id}
    until status is "ready", then pitch with POST /pitches using the returned brand_id.
    """
    try:
        handle = parse_instagram_handle(body.instagram)   # a typo is a 400 and does not use up the daily cap
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    ensure_room()   # 503 when the job queue is full: nothing is locked or counted yet
    bind = db.get_bind()
    if not usage_limits.try_acquire_lock(db, current_user.id, "own_brand", usage_limits.job_lock_stale_seconds()):
        # one active lookup per user: the same brand again (a double click) gets that job back, another brand waits
        active = None
        for attempt in range(15):   # a double click: the first request is still creating its job row, give it a moment (3 s at most)
            db.rollback()
            active = (db.query(OwnBrandJob).filter(OwnBrandJob.creator_profile_id == current_user.id, OwnBrandJob.status.in_(("queued", "running")))
                      .order_by(OwnBrandJob.created_at.desc()).first())
            if active is not None or attempt == 14:
                break
            time.sleep(0.2)
        if active is not None and active.instagram_handle == handle:
            return _to_response(db, refresh_stale(db, active))
        raise HTTPException(status_code=409, detail="A brand lookup is already running. Wait for it to finish, then look up the next one.")
    queued = False
    try:
        usage_limits.consume(db, current_user.id, "own_brands")   # 429 over the daily cap, before the Apify run is queued
        try:
            job = start_own_brand_job(db, current_user.id, body.instagram)
        except ValueError as exc:
            usage_limits.refund(db, current_user.id, "own_brands")
            raise HTTPException(status_code=400, detail=str(exc))
        if job.status == "queued":
            try:
                submit_user_job(run_own_brand_job, job.id, creator_id=current_user.id, locks=("own_brand",), bind=bind)
                queued = True
            except QueueFull:   # lost the race for the last place in the queue: the job is not run, and not counted
                job.status, job.stage, job.error = "failed", "Stopped", "The server is busy, please try again in a moment"
                db.commit()
                usage_limits.refund(db, current_user.id, "own_brands")
                raise busy_error()
        return _to_response(db, job)
    finally:
        if not queued:
            usage_limits.release_lock(bind, current_user.id, "own_brand")   # a queued job releases it itself when it ends


@router.get("/jobs/{job_id}", response_model=OwnBrandJobResponse)
def get_own_brand_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_completed_user),
):
    job = db.get(OwnBrandJob, job_id)
    if job is None or job.creator_profile_id != current_user.id:   # a foreign job id gets the same answer as a missing one
        raise HTTPException(status_code=404, detail="Job not found")
    return _to_response(db, refresh_stale(db, job))
