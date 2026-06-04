"""
pipeline/contacts.py

Stage 3 of the brand enrichment pipeline.
Reads brands WHERE contacts_fetched = false AND contacts_fetch_failed = false,
calls Apollo people search, scores by title relevance, inserts top-5 contacts,
and fills email gaps using Hunter email patterns.

Runs nightly at 2:15am via scheduler.py.
"""

import logging
from typing import Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from config import APOLLO_API_KEY, CONTACTS_BATCH_SIZE
from pipeline.db import Brand, Contact

logger = logging.getLogger(__name__)

# (keywords, priority_score) — lower score = higher priority
_TITLE_PRIORITIES: list[tuple[list[str], int]] = [
    (["partnerships"], 1),
    (["sponsorship"], 1),
    (["influencer"], 2),
    (["brand marketing"], 2),
    (["marketing manager"], 3),
    (["cmo", "founder"], 4),
]

_TITLE_KEYWORDS = [kw for keywords, _ in _TITLE_PRIORITIES for kw in keywords]


def _score_title(title: str) -> int:
    t = (title or "").lower()
    for keywords, score in _TITLE_PRIORITIES:
        if any(kw in t for kw in keywords):
            return score
    return 99


def _fetch_apollo_people(domain: str) -> list[dict]:
    try:
        resp = httpx.post(
            "https://api.apollo.io/v1/mixed_people/search",
            headers={"X-Api-Key": APOLLO_API_KEY},
            json={
                "organization_domains": [domain],
                "person_titles": _TITLE_KEYWORDS,
                "per_page": 10,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("people", [])
    except Exception:
        logger.exception("Apollo people search failed for domain '%s'", domain)
        return []


def _guess_email(first: str, last: str, pattern: str, domain: str) -> Optional[str]:
    if not pattern or not first or not last:
        return None
    email = pattern.replace("{first}", first.lower())
    email = email.replace("{last}", last.lower())
    email = email.replace("{f}", first[0].lower() if first else "")
    if "@" not in email:
        email = f"{email}@{domain}"
    return email


def run_contacts(db: Session, batch_size: int = CONTACTS_BATCH_SIZE) -> int:
    """
    Fetch decision-maker contacts for one batch of enriched brands.
    Returns the count of newly inserted contact rows.
    """
    brands = (
        db.execute(
            select(Brand)
            .where(Brand.contacts_fetched == False)  # noqa: E712
            .where(Brand.contacts_fetch_failed == False)  # noqa: E712
            .limit(batch_size)
        )
        .scalars()
        .all()
    )
    logger.info("Contacts batch: %d brands to process", len(brands))

    inserted_total = 0
    for brand in brands:
        people = _fetch_apollo_people(brand.domain)

        if not people:
            db.execute(
                update(Brand).where(Brand.id == brand.id).values(contacts_fetch_failed=True)
            )
            db.commit()
            logger.warning("No contacts returned by Apollo for '%s'", brand.domain)
            continue

        # Score all results, take top 5 by priority
        scored = sorted(
            [{**p, "_title_score": _score_title(p.get("title", ""))} for p in people],
            key=lambda x: x["_title_score"],
        )[:5]

        for person in scored:
            first = person.get("first_name", "")
            last = person.get("last_name", "")
            full_name = f"{first} {last}".strip() or person.get("name", "Unknown")
            email = person.get("email")
            guessed = False

            if not email and brand.email_pattern and brand.domain:
                email = _guess_email(first, last, brand.email_pattern, brand.domain)
                guessed = bool(email)

            stmt = (
                insert(Contact)
                .values(
                    brand_id=brand.id,
                    full_name=full_name,
                    title=person.get("title"),
                    title_score=person["_title_score"],
                    email=email,
                    linkedin_url=person.get("linkedin_url"),
                    apollo_email_status=person.get("email_status"),
                    email_guessed=guessed,
                )
                .on_conflict_do_nothing()
            )
            result = db.execute(stmt)
            inserted_total += result.rowcount

        db.execute(
            update(Brand).where(Brand.id == brand.id).values(contacts_fetched=True)
        )
        db.commit()
        logger.debug(
            "Contacts done for '%s' — %d people scored, %d inserted",
            brand.domain,
            len(scored),
            inserted_total,
        )

    logger.info(
        "Contacts done — %d contacts inserted across %d brands",
        inserted_total,
        len(brands),
    )
    return inserted_total
