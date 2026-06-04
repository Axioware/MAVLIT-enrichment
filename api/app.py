from fastapi import Depends, FastAPI
from pydantic import BaseModel
from sqladmin import Admin, ModelView
from sqlalchemy.orm import Session

from pipeline.db import Brand, BrandRaw, Contact, engine, get_db
from pipeline.seed import run_seed

app = FastAPI(
    title="Sponsorship Pipeline",
    description="Brand database enrichment pipeline API",
    version="0.1.0",
)


# SQLAdmin — browse all three tables at /admin

class BrandRawAdmin(ModelView, model=BrandRaw):
    name = "Brand Raw"
    name_plural = "Brands Raw"
    icon = "fa-solid fa-seedling"
    column_list = [
        BrandRaw.id,
        BrandRaw.name,
        BrandRaw.niche,
        BrandRaw.source,
        BrandRaw.enriched,
        BrandRaw.enrichment_failed,
        BrandRaw.created_at,
    ]
    column_searchable_list = [BrandRaw.name, BrandRaw.niche, BrandRaw.source]
    column_sortable_list = [BrandRaw.id, BrandRaw.niche, BrandRaw.enriched, BrandRaw.created_at]
    column_default_sort = [(BrandRaw.id, True)]
    page_size = 50


class BrandAdmin(ModelView, model=Brand):
    name = "Brand"
    name_plural = "Brands"
    icon = "fa-solid fa-building"
    column_list = [
        Brand.id,
        Brand.name,
        Brand.domain,
        Brand.industry,
        Brand.employee_count,
        Brand.hq_country,
        Brand.enrichment_source,
        Brand.contacts_fetched,
        Brand.contacts_fetch_failed,
        Brand.enriched_at,
    ]
    column_searchable_list = [Brand.name, Brand.domain, Brand.industry]
    column_sortable_list = [Brand.id, Brand.domain, Brand.contacts_fetched, Brand.enriched_at]
    column_default_sort = [(Brand.id, True)]
    page_size = 50


class ContactAdmin(ModelView, model=Contact):
    name = "Contact"
    name_plural = "Contacts"
    icon = "fa-solid fa-user"
    column_list = [
        Contact.id,
        Contact.full_name,
        Contact.title,
        Contact.title_score,
        Contact.email,
        Contact.email_verified,
        Contact.email_status,
        Contact.email_guessed,
        Contact.outreach_sent,
        Contact.created_at,
    ]
    column_searchable_list = [Contact.full_name, Contact.email, Contact.title]
    column_sortable_list = [
        Contact.id,
        Contact.title_score,
        Contact.email_verified,
        Contact.created_at,
    ]
    column_default_sort = [(Contact.id, True)]
    page_size = 50


admin = Admin(app, engine)
admin.add_view(BrandRawAdmin)
admin.add_view(BrandAdmin)
admin.add_view(ContactAdmin)


# API endpoints

class SeedRequest(BaseModel):
    niche: str
    use_google: bool = False
    limit: int | None = None


class SeedResponse(BaseModel):
    niche: str
    inserted: int
    message: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/seed", response_model=SeedResponse)
def seed_niche(body: SeedRequest, db: Session = Depends(get_db)):
    inserted = run_seed(
        niche=body.niche,
        db=db,
        use_google=body.use_google,
        limit=body.limit,
    )
    return SeedResponse(
        niche=body.niche,
        inserted=inserted,
        message=f"Seeded {inserted} new brands for niche '{body.niche}'",
    )
