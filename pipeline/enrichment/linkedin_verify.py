"""
pipeline/enrichment/linkedin_verify.py

For brand_contacts rows that Apollo already enriched with a real email
(is_enriched=True) and that carry a linkedin_url, checks whether that
person's LinkedIn profile still shows them as currently employed at the
brand — a contact who moved on since Apollo last indexed them is a wasted
pitch.

Uses the "LinkedIn Profile Scraper" Apify actor (G3EZsXsaGaLDcc4uc,
https://apify.com/calm_builder/linkedin-profile-scraper) — reads public
LinkedIn profile pages as an unauthenticated visitor (no login), returns
one JSON record per profile including a ready-made currentCompany field
({"name": ..., "url": ...}) that IS the profile's own "current employer"
signal — this module doesn't need to infer it from the experience/work-
history list itself.

Pipeline:
  1. FETCH  -> one Apify actor call per batch, passing every pending
              contact's linkedin_url in a single run_input["profiles"]
              list (the actor supports many profiles per call) rather than
              one run per contact — cheaper (one ~$0.001 actor-start
              charge instead of N) and fewer round trips. Results are
              matched back to contacts by the stable /in/<identifier> slug
              (see _linkedin_identifier), not the raw URL string — the
              actor normalizes a stored "http://" URL to "https://" (and
              possibly other cosmetic changes) in its own output, so exact
              URL matching silently drops every result.
  2. COMPARE -> for each contact whose profile fetch succeeded, an LLM
              call (linkedin_company_match) judges whether currentCompany
              is the same real-world company as the brand — allowing for
              legal-suffix/punctuation/parent-brand differences, but never
              matching an unrelated same-industry company. See that
              prompt for the exact rules.

Writes, per contact:
  linkedin_verified_at — when this check last ran (NULL = never)
  still_at_brand       — True/False once checked; stays NULL if the
                          profile fetch itself failed (private/deleted/
                          actor error) — genuinely unknown, distinct from a
                          checked "no longer there" (False).

Cost-conscious by design: this actor is pay-per-profile (~$0.005-0.01
each depending on plan tier, per Apify's own pricing) — only runs against
is_enriched=True contacts (the ~5 per brand Apollo already spent real
credits revealing an email for), never the full up-to-50 stored candidate
list, and never re-checks a contact once linkedin_verified_at is set
(pass contact_id to force a re-check).
"""

import logging
from urllib.parse import urlparse

from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from config import APIFY_TOKEN, OPENAI_KEY
from pipeline.db import BrandContact, Prompt
from pipeline.helpers.apify import run_apify_actor
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template
from pipeline.helpers.prompts import LINKEDIN_COMPANY_MATCH_PROMPT_NAME, LINKEDIN_COMPANY_MATCH_DEFAULT_PROMPT

logger = logging.getLogger(__name__)

_ACTOR_ID = "G3EZsXsaGaLDcc4uc"  # linkedin-profile-scraper (calm_builder) — public, no-login profile reads


def _linkedin_identifier(url: str) -> str:
    """
    Extract the stable /in/<identifier> slug from a LinkedIn profile URL,
    lowercased, ignoring scheme/www/query string/trailing slash. Confirmed
    live: the actor normalizes a stored "http://...” URL to "https://...”
    (and may make other cosmetic changes) in its own linkedinUrl output, so
    matching results back to contacts by exact URL string silently drops
    every match — this stable slug is what actually survives that.
    """
    if not url:
        return ""
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    if len(parts) >= 2 and parts[0].lower() == "in":
        return parts[1].lower()
    return path.lower()


def _get_prompt(db: Session, name: str, default: str) -> str:
    row = db.query(Prompt).filter(Prompt.name == name).first()
    return row.content if row else default


def _llm_company_match(db: Session, brand_name: str, brand_domain: str, current_company: dict) -> bool:
    """
    current_company is the raw {"name":..., "url":...} dict from the actor
    (or {} if the profile showed none). Returns False whenever there's
    nothing to compare (empty current_company) or the LLM isn't confident
    it's the same company — never guesses "still there" on weak evidence.
    """
    name = (current_company or {}).get("name") or ""
    if not name:
        return False
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping LinkedIn company-match LLM call")
        return False

    prompt = fill_template(
        _get_prompt(db, LINKEDIN_COMPANY_MATCH_PROMPT_NAME, LINKEDIN_COMPANY_MATCH_DEFAULT_PROMPT),
        brand_name=brand_name or "unknown",
        brand_domain=brand_domain or "none",
        current_company=name,
        current_company_url=(current_company or {}).get("url") or "none",
    )
    result = call_gpt_json(prompt, context=f"linkedin company match: {name!r} vs {brand_name!r}")
    return bool(result.get("match", False)) if isinstance(result, dict) else False


def _apply_verification(db: Session, contact: BrandContact, item: dict | None) -> None:
    if not item or not item.get("success"):
        # Profile fetch itself failed (private/deleted/actor error) —
        # genuinely unknown, not "confirmed no longer there". Still marks
        # linkedin_verified_at so a permanently-unreachable profile isn't
        # retried forever; pass contact_id to force a retry later.
        contact.linkedin_verified_at = func.now()
        contact.still_at_brand = None
        db.commit()
        return

    current_company = item.get("currentCompany") or {}
    company_name = current_company.get("name") or None

    contact.still_at_brand = _llm_company_match(db, contact.brand_raw.name, contact.brand_raw.domain, current_company)
    contact.linkedin_verified_at = func.now()
    db.commit()

    logger.info(
        "LinkedIn verify: contact_id=%s '%s' at '%s' — LinkedIn shows '%s' -> still_at_brand=%s",
        contact.id, contact.full_name, contact.brand_raw.name, company_name or "(none shown)", contact.still_at_brand,
    )


def run_linkedin_verify(db: Session, limit: int = 20, contact_id: int | None = None) -> int:
    """
    Verifies current employment for pending brand_contacts rows (is_enriched
    =True, linkedin_url set, linkedin_verified_at IS NULL). Pass contact_id
    to target one specific brand_contacts row directly — bypasses the
    is_enriched and already-verified filters, so you can re-check a contact
    regardless of its current state.

    Returns number of contacts processed (verified or attempted-and-failed).
    """
    if not APIFY_TOKEN:
        logger.warning("APIFY_TOKEN not set — skipping LinkedIn employment verification")
        return 0

    query = db.query(BrandContact).filter(BrandContact.linkedin_url.isnot(None))
    if contact_id is not None:
        query = query.filter(BrandContact.id == contact_id)
    else:
        query = query.filter(
            BrandContact.is_enriched.is_(True),
            BrandContact.linkedin_verified_at.is_(None),
        )

    contacts: list[BrandContact] = query.limit(limit).all()
    if not contacts:
        logger.info("LinkedIn verify: no pending contacts")
        return 0

    logger.info("LinkedIn verify: checking %d contact(s)", len(contacts))

    urls = [c.linkedin_url for c in contacts]
    items = run_apify_actor(_ACTOR_ID, {"profiles": urls}, label="LinkedIn employment verify") or []
    by_identifier = {
        item.get("publicIdentifier", "").lower(): item
        for item in items if item.get("publicIdentifier")
    }

    processed = 0
    for contact in contacts:
        item = by_identifier.get(_linkedin_identifier(contact.linkedin_url))
        _apply_verification(db, contact, item)
        processed += 1

    logger.info("LinkedIn verify: %d contact(s) processed", processed)
    return processed
