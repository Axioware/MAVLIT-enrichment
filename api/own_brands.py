from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.auth import get_current_user
from pipeline.db import BrandRaw, CreatorProfile, OwnBrandJob, get_db
from pipeline.own_brand import refresh_stale, run_own_brand_job, start_own_brand_job

router = APIRouter(prefix="/own-brands", tags=["own-brands"])


class OwnBrandRequest(BaseModel):
    instagram: str   # the brand's Instagram URL or handle


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
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_current_user),
):
    """
    "My own brand" on the pitch page. Adds the brand's Instagram account to MAVLIT (or reuses it if
    it is already there), then in the background reads its latest 40 posts and writes the brand profile
    (name, description, niche, target audience, latest product). Poll GET /own-brands/jobs/{job_id}
    until status is "ready", then pitch with POST /pitches using the returned brand_id.
    """
    try:
        job = start_own_brand_job(db, current_user.id, body.instagram)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if job.status == "queued":
        background_tasks.add_task(run_own_brand_job, job.id)
    return _to_response(db, job)


@router.get("/jobs/{job_id}", response_model=OwnBrandJobResponse)
def get_own_brand_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_current_user),
):
    job = db.get(OwnBrandJob, job_id)
    if job is None or job.creator_profile_id != current_user.id:   # a foreign job id gets the same answer as a missing one
        raise HTTPException(status_code=404, detail="Job not found")
    return _to_response(db, refresh_stale(db, job))
