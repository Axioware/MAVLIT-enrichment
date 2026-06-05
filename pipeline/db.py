from sqlalchemy import Boolean, Column, ForeignKey, Integer, Text, TIMESTAMP, create_engine
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.sql import func

from config import DATABASE_URL

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


class BrandRaw(Base):
    __tablename__ = "brands_raw"

    id               = Column(Integer, primary_key=True)
    name             = Column(Text, nullable=False)
    name_normalized  = Column(Text, unique=True, nullable=False)
    niche            = Column(Text, nullable=False)
    source           = Column(Text, nullable=False)
    source_url       = Column(Text)
    # Official website (P856) resolved at seed time
    website          = Column(Text)
    domain           = Column(Text)
    enriched         = Column(Boolean, nullable=False, server_default="false", default=False)
    enrichment_failed= Column(Boolean, nullable=False, server_default="false", default=False)
    country          = Column(Text)
    headquarters     = Column(Text)
    location         = Column(Text)
    operating_area   = Column(Text)
    created_at       = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Brand(Base):
    __tablename__ = "brands"

    id               = Column(Integer, primary_key=True)
    raw_id           = Column(Integer, ForeignKey("brands_raw.id"))
    name             = Column(Text, nullable=False)
    domain           = Column(Text, unique=True)
    industry         = Column(Text)
    employee_count   = Column(Integer)
    linkedin_url     = Column(Text)
    hq_country       = Column(Text)
    email_pattern    = Column(Text)
    enrichment_source= Column(Text)
    contacts_fetched = Column(Boolean, nullable=False, server_default="false", default=False)
    contacts_fetch_failed = Column(Boolean, nullable=False, server_default="false", default=False)
    enriched_at      = Column(TIMESTAMP(timezone=True), server_default=func.now())


class Contact(Base):
    __tablename__ = "contacts"

    id                  = Column(Integer, primary_key=True)
    brand_id            = Column(Integer, ForeignKey("brands.id"))
    full_name           = Column(Text, nullable=False)
    title               = Column(Text)
    title_score         = Column(Integer)
    email               = Column(Text, unique=True)
    linkedin_url        = Column(Text, unique=True)
    apollo_email_status = Column(Text)
    email_verified      = Column(Boolean, nullable=False, server_default="false", default=False)
    email_status        = Column(Text)
    email_score         = Column(Integer)
    email_guessed       = Column(Boolean, nullable=False, server_default="false", default=False)
    verified_at         = Column(TIMESTAMP(timezone=True))
    outreach_sent       = Column(Boolean, nullable=False, server_default="false", default=False)
    created_at          = Column(TIMESTAMP(timezone=True), server_default=func.now())


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _brand_raw_fields(b: dict) -> dict:
    """Extract all optional BrandRaw fields from a seed row dict."""
    return {
        "website":        b.get("website") or None,
        "domain":         b.get("domain") or None,
        "country":        b.get("country"),
        "headquarters":   b.get("headquarters"),
        "location":       b.get("location"),
        "operating_area": b.get("operating_area"),
    }


def insert_brand(db: Session, brand: dict) -> bool:
    stmt = (
        insert(BrandRaw)
        .values(
            name=brand["name"],
            name_normalized=brand["normalized"],
            niche=brand["niche"],
            source=brand["source"],
            source_url=brand.get("source_url"),
            **_brand_raw_fields(brand),
        )
        .on_conflict_do_nothing(index_elements=["name_normalized"])
    )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount == 1


def insert_brands_batch(db: Session, brands: list[dict]) -> int:
    if not brands:
        return 0

    stmt = (
        insert(BrandRaw)
        .values(
            [
                {
                    "name":            b["name"],
                    "name_normalized": b["normalized"],
                    "niche":           b["niche"],
                    "source":          b["source"],
                    "source_url":      b.get("source_url"),
                    **_brand_raw_fields(b),
                }
                for b in brands
            ]
        )
        .on_conflict_do_nothing(index_elements=["name_normalized"])
    )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount