"""One-time enrichment of existing pending Apollo contact rows.

For every brand, selects up to five existing brand_contacts rows that:
  - are not enriched yet
  - have sponsorship_contact_confidence >= 60
  - have an Apollo person ID

The rows are processed highest-confidence first within each brand. This
script does not run an Apollo search and does not create new contact rows; it
only enriches contacts already stored in brand_contacts.

Run:
    python run_apollo_pending_contacts_once.py

Optional preview without Apollo calls:
    python run_apollo_pending_contacts_once.py --dry-run
"""

import argparse
import logging

from sqlalchemy import or_

from config import APOLLO_API_KEY
from pipeline.db import BrandContact, SessionLocal
from pipeline.enrichment.apollo_contacts import _ApolloAuthError, _enrich_person

_MAX_CONTACTS_PER_BRAND = 5
_MIN_CONFIDENCE = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def _join_values(value) -> str | None:
    if not value:
        return None
    if isinstance(value, list):
        return "; ".join(str(item) for item in value if item)
    return str(value)


def _apply_person(contact: BrandContact, person: dict) -> None:
    first_name = person.get("first_name") or ""
    last_name = person.get("last_name") or person.get("last_name_obfuscated") or ""
    full_name = f"{first_name} {last_name}".strip()

    if full_name:
        contact.full_name = full_name
    contact.title = person.get("title") or contact.title
    contact.departments = _join_values(person.get("departments")) or contact.departments
    contact.subdepartments = _join_values(person.get("subdepartments")) or contact.subdepartments
    contact.functions = _join_values(person.get("functions")) or contact.functions
    contact.seniority = person.get("seniority") or contact.seniority
    contact.email = person.get("email") or contact.email
    contact.email_status = person.get("email_status") or contact.email_status
    contact.phone = person.get("phone") or contact.phone
    contact.linkedin_url = person.get("linkedin_url") or contact.linkedin_url
    contact.city = person.get("city") or contact.city
    contact.state = person.get("state") or contact.state
    contact.country = person.get("country") or contact.country
    contact.is_enriched = True


def main(dry_run: bool = False) -> int:
    if not APOLLO_API_KEY:
        logger.error("APOLLO_API_KEY is not set")
        return 1

    db = SessionLocal()
    try:
        candidates = (
            db.query(BrandContact)
            .filter(
                or_(
                    BrandContact.is_enriched.is_(False),
                    BrandContact.is_enriched.is_(None),
                ),
                BrandContact.sponsorship_contact_confidence >= _MIN_CONFIDENCE,
                BrandContact.apollo_person_id.isnot(None),
            )
            .order_by(
                BrandContact.brand_raw_id.asc(),
                BrandContact.sponsorship_contact_confidence.desc(),
                BrandContact.id.asc(),
            )
            .all()
        )
        contacts = []
        selected_by_brand = {}
        for contact in candidates:
            selected_count = selected_by_brand.get(contact.brand_raw_id, 0)
            if selected_count >= _MAX_CONTACTS_PER_BRAND:
                continue
            contacts.append(contact)
            selected_by_brand[contact.brand_raw_id] = selected_count + 1

        if not contacts:
            logger.info("No unenriched contacts with confidence >= %d found", _MIN_CONFIDENCE)
            return 0

        logger.info(
            "Selected %d contact(s) across %d brand(s) for one-time enrichment",
            len(contacts),
            len(selected_by_brand),
        )
        if dry_run:
            for contact in contacts:
                logger.info(
                    "DRY RUN contact_id=%d brand_id=%d person_id=%s confidence=%d",
                    contact.id,
                    contact.brand_raw_id,
                    contact.apollo_person_id,
                    contact.sponsorship_contact_confidence,
                )
            return 0

        processed = 0
        enriched = 0
        for contact in contacts:
            logger.info(
                "Enriching contact_id=%d person_id=%s confidence=%d",
                contact.id,
                contact.apollo_person_id,
                contact.sponsorship_contact_confidence,
            )
            try:
                person = _enrich_person(str(contact.apollo_person_id), reveal_phone=False)
            except _ApolloAuthError:
                db.rollback()
                logger.exception("Apollo authentication failed; stopping without processing remaining contacts")
                return 1

            processed += 1
            if not person:
                logger.warning("No Apollo person returned for contact_id=%d", contact.id)
                continue

            _apply_person(contact, person)
            enriched += 1
            db.commit()

        logger.info(
            "One-time Apollo enrichment complete: %d attempted, %d enriched, maximum_per_brand=%d",
            processed,
            enriched,
            _MAX_CONTACTS_PER_BRAND,
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the five selected contacts without calling Apollo",
    )
    args = parser.parse_args()
    raise SystemExit(main(dry_run=args.dry_run))
