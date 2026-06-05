import uuid
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqladmin import Admin, ModelView
from sqlalchemy import text

from pipeline.db import Base, Brand, BrandRaw, Contact, SessionLocal, engine
from pipeline.seed import run_seed


def _run_migrations() -> None:
    """Create tables and apply column migrations before accepting requests."""
    Base.metadata.create_all(bind=engine)
    stmts = [
        # Original columns (idempotent)
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS enrichment_failed BOOLEAN NOT NULL DEFAULT false",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS country TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS headquarters TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS location TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS operating_area TEXT",
        # Website / domain columns sourced from Wikidata P856
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS website TEXT",
        "ALTER TABLE brands_raw ADD COLUMN IF NOT EXISTS domain  TEXT",
    ]
    with engine.connect() as conn:
        for sql in stmts:
            conn.execute(text(sql))
        conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _run_migrations()
    yield


app = FastAPI(
    title="Sponsorship Pipeline",
    description="Brand database enrichment pipeline API",
    version="0.1.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# SQLAdmin — /admin
# ---------------------------------------------------------------------------

class BrandRawAdmin(ModelView, model=BrandRaw):
    name         = "Brand Raw"
    name_plural  = "Brands Raw"
    icon         = "fa-solid fa-seedling"
    column_list  = [
        BrandRaw.id,
        BrandRaw.name,
        BrandRaw.niche,
        BrandRaw.source,
        # Website columns — populated from Wikidata P856 at seed time
        BrandRaw.website,
        BrandRaw.domain,
        # Geo context
        BrandRaw.country,
        BrandRaw.headquarters,
        BrandRaw.location,
        BrandRaw.operating_area,
        # Status
        BrandRaw.enriched,
        BrandRaw.enrichment_failed,
        BrandRaw.created_at,
    ]
    column_searchable_list = [
        BrandRaw.name,
        BrandRaw.niche,
        BrandRaw.source,
        BrandRaw.website,
        BrandRaw.domain,
        BrandRaw.country,
        BrandRaw.headquarters,
        BrandRaw.location,
        BrandRaw.operating_area,
    ]
    column_sortable_list = [
        BrandRaw.id,
        BrandRaw.niche,
        BrandRaw.domain,
        BrandRaw.country,
        BrandRaw.operating_area,
        BrandRaw.enriched,
        BrandRaw.created_at,
    ]
    column_default_sort = [(BrandRaw.id, True)]
    page_size = 50


class BrandAdmin(ModelView, model=Brand):
    name         = "Brand"
    name_plural  = "Brands"
    icon         = "fa-solid fa-building"
    column_list  = [
        Brand.id, Brand.name, Brand.domain, Brand.industry,
        Brand.employee_count, Brand.hq_country, Brand.enrichment_source,
        Brand.contacts_fetched, Brand.contacts_fetch_failed, Brand.enriched_at,
    ]
    column_searchable_list = [Brand.name, Brand.domain, Brand.industry]
    column_sortable_list   = [Brand.id, Brand.domain, Brand.contacts_fetched, Brand.enriched_at]
    column_default_sort    = [(Brand.id, True)]
    page_size = 50


class ContactAdmin(ModelView, model=Contact):
    name         = "Contact"
    name_plural  = "Contacts"
    icon         = "fa-solid fa-user"
    column_list  = [
        Contact.id, Contact.full_name, Contact.title, Contact.title_score,
        Contact.email, Contact.email_verified, Contact.email_status,
        Contact.email_guessed, Contact.outreach_sent, Contact.created_at,
    ]
    column_searchable_list = [Contact.full_name, Contact.email, Contact.title]
    column_sortable_list   = [Contact.id, Contact.title_score, Contact.email_verified, Contact.created_at]
    column_default_sort    = [(Contact.id, True)]
    page_size = 50


admin = Admin(app, engine)
admin.add_view(BrandRawAdmin)
admin.add_view(BrandAdmin)
admin.add_view(ContactAdmin)

# ---------------------------------------------------------------------------
# API models
# ---------------------------------------------------------------------------

class SeedRequest(BaseModel):
    niche:          str
    use_google:     bool        = False
    limit:          int | None  = None
    country:        str | None  = None
    headquarters:   str | None  = None
    location:       str | None  = None
    operating_area: str | None  = None

# ---------------------------------------------------------------------------
# Background job store (in-memory; single-process)
# ---------------------------------------------------------------------------

_jobs: dict[str, dict] = {}


def _run_seed_job(job_id: str, body: SeedRequest) -> None:
    db = SessionLocal()
    try:
        inserted = run_seed(
            niche=body.niche,
            db=db,
            use_google=body.use_google,
            limit=body.limit,
            country=body.country,
            headquarters=body.headquarters,
            location=body.location,
            operating_area=body.operating_area,
        )
        _jobs[job_id].update({"status": "done", "inserted": inserted})
    except Exception as exc:
        _jobs[job_id].update({"status": "error", "error": str(exc)})
    finally:
        db.close()

# ---------------------------------------------------------------------------
# Frontend — /
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/", include_in_schema=False)
def frontend():
    return FileResponse("frontend/index.html")


class SeedJobResponse(BaseModel):
    job_id:  str
    status:  str
    niche:   str
    message: str


class SeedStatusResponse(BaseModel):
    status:   str
    niche:    str
    inserted: int
    error:    str | None = None

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/seed", response_model=SeedJobResponse)
def seed_niche(body: SeedRequest, background_tasks: BackgroundTasks):
    """
    Kick off a seed job in the background and return a job_id immediately.
    Poll GET /seed/status/{job_id} to track progress.
    """
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {"status": "running", "niche": body.niche, "inserted": 0}
    background_tasks.add_task(_run_seed_job, job_id, body)
    return SeedJobResponse(
        job_id=job_id,
        status="running",
        niche=body.niche,
        message=f"Seeding '{body.niche}' started",
    )


@app.get("/seed/status/{job_id}", response_model=SeedStatusResponse)
def seed_status(job_id: str):
    """Poll every 2 s until status is 'done' or 'error'."""
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return SeedStatusResponse(
        status=job["status"],
        niche=job["niche"],
        inserted=job.get("inserted", 0),
        error=job.get("error"),
    )