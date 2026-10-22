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
  2. DECIDE  -> for each contact whose profile fetch succeeded, an LLM
              call (linkedin_company_match) reads currentCompany AND the
              profile's work-experience list and answers one of:
                still_there - current employer (or a current role) is the brand -> True
                left        - the experience shows a role AT the brand that has ended and no
                              current role there -> False
                unknown     - anything else -> NULL. Notably: they now work elsewhere but the
                              brand is not in their experience at all. They may just not have
                              listed that role, so it is never turned into "left".
              See that prompt for the exact rules.

Writes, per contact:
  linkedin_verified_at — when this check last ran (NULL = never)
  still_at_brand       — True (works there now) / False (experience shows they left) /
                          NULL when unknown: the profile fetch failed (private/deleted/
                          actor error) or the profile does not show enough to say.
  still_at_brand_confidence — 0-100, how sure the LLM was of that verdict;
                          NULL when no LLM call was made.

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
from pipeline.helpers.prompts import (
    LINKEDIN_COMPANY_MATCH_DEFAULT_PROMPT,
    LINKEDIN_COMPANY_MATCH_PROMPT_NAME,
)

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


_MAX_EXPERIENCE_ENTRIES = 12


def _experience_text(experience: list | None) -> str:
    """The profile's work history as one line per role, e.g. 'Marketing Manager at Nike (2015 - 2020)'."""
    lines = []
    for role in (experience or [])[:_MAX_EXPERIENCE_ENTRIES]:
        if not isinstance(role, dict) or not role.get("company"):
            continue
        dates = role.get("dateRange") or "dates not shown"
        marker = " [current]" if role.get("current") else ""
        title = role.get("title") or "role not shown"
        lines.append(f"- {title} at {role['company']} ({dates}){marker}")
    return "\n".join(lines) or "none shown"


_STATUS_TO_STILL_AT_BRAND = {"still_there": True, "left": False, "unknown": None}


def _llm_employment_status(
    db: Session, brand_name: str, brand_domain: str, current_company: dict, experience: list | None,
) -> tuple[bool | None, int | None]:
    """
    Returns (still_at_brand, confidence).
      True  - the profile shows them working at the brand now.
      False - their experience shows a role at the brand that has ended and no current role there.
      None  - unknown: the profile shows nothing to decide on, or they now work elsewhere but the
              brand is not in their experience at all (they may just not have listed it) - that is
              never treated as "left".
    confidence is the LLM's own 0-100 certainty; None when no LLM call was made or it gave no number.
    """
    name = (current_company or {}).get("name") or ""
    experience_text = _experience_text(experience)
    if not name and experience_text == "none shown":
        return None, None
    if not OPENAI_KEY:
        logger.warning("OPENAI_KEY not set — skipping LinkedIn employment LLM call")
        return None, None

    prompt = fill_template(
        _get_prompt(db, LINKEDIN_COMPANY_MATCH_PROMPT_NAME, LINKEDIN_COMPANY_MATCH_DEFAULT_PROMPT),
        brand_name=brand_name or "unknown",
        brand_domain=brand_domain or "none",
        current_company=name or "none shown",
        current_company_url=(current_company or {}).get("url") or "none",
        experience=experience_text,
    )
    result = call_gpt_json(prompt, context=f"linkedin employment: {name!r} vs {brand_name!r}")
    if not isinstance(result, dict):
        return None, None
    still_at_brand = _STATUS_TO_STILL_AT_BRAND.get(str(result.get("status", "")).strip().lower())
    confidence = result.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = None
    else:
        confidence = max(0, min(100, round(confidence)))
    return still_at_brand, confidence


def _apply_verification(db: Session, contact: BrandContact, item: dict | None) -> None:
    if not item or not item.get("success"):
        # Profile fetch itself failed (private/deleted/actor error) —
        # genuinely unknown, not "confirmed no longer there". Still marks
        # linkedin_verified_at so a permanently-unreachable profile isn't
        # retried forever; pass contact_id to force a retry later.
        contact.linkedin_verified_at = func.now()
        contact.still_at_brand = None
        contact.still_at_brand_confidence = None
        db.commit()
        return

    current_company = item.get("currentCompany") or {}
    company_name = current_company.get("name") or None

    contact.still_at_brand, contact.still_at_brand_confidence = _llm_employment_status(
        db, contact.brand_raw.name, contact.brand_raw.domain, current_company, item.get("experience"),
    )
    contact.linkedin_verified_at = func.now()
    db.commit()

    logger.info(
        "LinkedIn verify: contact_id=%s '%s' at '%s' — LinkedIn shows '%s' -> still_at_brand=%s (confidence %s)",
        contact.id, contact.full_name, contact.brand_raw.name, company_name or "(none shown)",
        contact.still_at_brand, contact.still_at_brand_confidence,
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
