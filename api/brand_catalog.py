"""
api/brand_catalog.py

Unauthenticated internal browsing tool (same tier as add-creators.html /
the brand seeder — not the creator-portal-authenticated api/brands.py) for
scrolling through every brands_raw row and, per brand, seeing example Meta
Ad cards.

The "Meta Ad Examples" section is intentionally the SAME hardcoded set on
every single brand, regardless of what (if anything) that brand actually
has in meta_ads — this is a fixed UI example/placeholder, not a per-brand
data query. Real meta_ads rows exist for very few brands right now, so a
real per-brand lookup would show "no ads" almost everywhere; these are
kept as real ad content (originally pulled from meta_ads) purely as a
static example of what the card layout looks like with real data in it.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from pipeline.db import BrandRaw, get_db

router = APIRouter(prefix="/api/brand-catalog", tags=["brand-catalog"])

# Same 3 example ads shown on every brand's detail page — see module
# docstring for why this isn't a per-brand meta_ads query.
_HARDCODED_META_ADS = [
    {
        "id": 105121,
        "page_name": "Oxfield books",
        "ad_creative_bodies": [
            "CHENNAI, INDIA. Seven months after the bombing.\nIvani Mishra thought she was safe. She was wrong.\nSpecial forces. Snipers. Drones. And something older in the shadows — something that doesn't die. Something that harvested her father and now hunts her brother.\nThe Watchers have never left. They've only been waiting.\nA mysterious voice on her comms gives her two choices:\nRun and live. Or stay and die with everyone she loves.\n\nTHE VANDERBILT DECEPTION: PART 1\nSplits the difference between Clancy and Crichton. Four missions. Four continents. One truth buried deep enough to end the world.\nNow available for pre-order.\n📖 June 29, 2026 → a.co/d/0eQdhGV0\n#TheVanderBiltDeception #paranormalthriller #conspiracythriller #BookTok",
        ],
        "publisher_platforms": ["facebook", "instagram", "audience_network", "messenger"],
        "start_date": "2026-05-15",
        "end_date": "2026-05-22",
        "impressions": {"lower_bound": "5000", "upper_bound": "9999"},
        "spend": {"lower_bound": "100", "upper_bound": "199"},
        "currency": "USD",
    },
    {
        "id": 105122,
        "page_name": "Oxfield books",
        "ad_creative_bodies": [
            "CHENNAI, INDIA. Seven months after the bombing.\nIvani Mishra thought she was safe. She was wrong.\nSpecial forces. Snipers. Drones. And something older in the shadows — something that doesn't die. Something that harvested her father and now hunts her brother.\nThe Watchers have never left. They've only been waiting.\nA mysterious voice on her comms gives her two choices:\nRun and live. Or stay and die with everyone she loves.\n\nTHE VANDERBILT DECEPTION: PART 1\nSplits the difference between Clancy and Crichton. Four missions. Four continents. One truth buried deep enough to end the world.\nNow available for pre-order.\n📖 June 29, 2026 → a.co/d/0eQdhGV0\n#TheVanderBiltDeception #paranormalthriller #conspiracythriller #BookTok",
        ],
        "publisher_platforms": ["facebook", "instagram", "audience_network", "messenger"],
        "start_date": "2026-05-14",
        "end_date": "2026-05-15",
        "impressions": {"lower_bound": "1000", "upper_bound": "1999"},
        "spend": {"lower_bound": "0", "upper_bound": "99"},
        "currency": "USD",
    },
    {
        "id": 105123,
        "page_name": "Brenda Siegel for Governor",
        "ad_creative_bodies": [
            "Thank you Ellen Oxfeld for this letter and thank you for being part of our movement for change.\n\n"
            "\"I will be supporting Brenda Siegel in the upcoming Democratic primary. In my mind, Brenda has shown "
            "her commitment through her life's work and consistent advocacy for key policies that would achieve "
            "greater economic security, social justice and environmental sustainability for Vermonters.\"\n\n"
            "https://www.benningtonbanner.com/stories/letter-siegels-advocacy-would-serve-vt-well,609647?",
        ],
        "publisher_platforms": ["facebook", "instagram"],
        "start_date": "2020-07-22",
        "end_date": "2020-07-26",
        "impressions": {"lower_bound": "1000", "upper_bound": "1999"},
        "spend": {"lower_bound": "0", "upper_bound": "99"},
        "currency": "USD",
    },
]


@router.get("")
def list_brands(
    q: str | None = Query(None, description="Search brand name (case-insensitive substring)"),
    niche: str | None = Query(None, description="Exact brands_raw.niche match"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(BrandRaw).filter(BrandRaw.name.isnot(None))
    if q:
        query = query.filter(BrandRaw.name.ilike(f"%{q.strip()}%"))
    if niche:
        query = query.filter(BrandRaw.niche == niche)

    total = query.count()
    rows = query.order_by(BrandRaw.name.asc()).offset(offset).limit(limit).all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "brands": [
            {
                "id": b.id,
                "name": b.name,
                "niche": b.niche,
                "website": b.website,
                "domain": b.domain,
                "country": b.country,
            }
            for b in rows
        ],
    }


@router.get("/niches")
def list_niches(db: Session = Depends(get_db)):
    """Distinct niche values currently in use — for the search page's filter dropdown."""
    rows = (
        db.query(BrandRaw.niche)
        .filter(BrandRaw.niche.isnot(None))
        .distinct()
        .order_by(BrandRaw.niche.asc())
        .all()
    )
    return {"niches": [r[0] for r in rows]}


@router.get("/{brand_id}")
def get_brand(brand_id: int, db: Session = Depends(get_db)):
    brand = db.query(BrandRaw).filter(BrandRaw.id == brand_id).first()
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")

    return {
        "id": brand.id,
        "name": brand.name,
        "niche": brand.niche,
        "description": brand.description,
        "website": brand.website,
        "domain": brand.domain,
        "country": brand.country,
        "headquarters": brand.headquarters,
        "instagram_handle": brand.instagram_handle,
        "youtube_channel_id": brand.youtube_channel_id,
        "facebook_page": brand.facebook_page,
        "brand_tier": brand.brand_tier,
        "geo_reach_score": brand.geo_reach_score,
        "geo_reach_label": brand.geo_reach_label,
        # Same fixed example set on every brand — see module docstring.
        "meta_ads_total": len(_HARDCODED_META_ADS),
        "meta_ads": _HARDCODED_META_ADS,
    }
