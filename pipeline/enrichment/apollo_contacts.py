"""
pipeline/enrichment/apollo_contacts.py

Finds up to 5 best marketing/sponsorship contacts for high-scoring brands
and stores them in brand_contacts, ranked best-first — gives a content
creator multiple people to try, not just one. Also updates
brand_match_profile's contact routing fields (has_marketing_contact,
contact_mode, best_contact_title_score) per the matching design doc, based
on the top-ranked (rank=1) contact.

Runs only for brands in initial_brand_score with total_score >= 50.

Pipeline (adapted from apollo_sponsorship_finder.py, minus the CSV/testing bits):
  1) SEARCH  -> Apollo /api/v1/mixed_people/api_search. FREE (0 credits).
                Sweeps the whole marketing department via a broad title list
                + include_similar_titles=True — Apollo has no confirmed
                person_departments= input filter (department only comes back
                as an OUTPUT field after enrichment), so a title-keyword
                sweep is the closest equivalent to "all of marketing".
  2) RANK    -> Mistral (pipeline.helpers.llm.call_mistral_json) ranks the
                candidates and picks up to TOP_N people most likely to
                personally own sponsorship/influencer-marketing budget
                decisions, from the free search results. Costs a fraction of
                a cent of Mistral usage, not Apollo credits. Falls back to
                keyword title-matching if MISTRAL_API_KEY isn't set, or if
                Mistral's picks don't match any real candidate.
  3) ENRICH  -> Apollo /v1/people/match. Only Mistral's picks get enriched to
                reveal a real name/email — this is the only step that costs
                Apollo credits, and it only ever runs once per picked person
                (up to TOP_N credits per brand, never more).

Credit-conscious by design:
  - Search is free — no need to ration it.
  - At most TOP_N Apollo enrich calls per brand (default 5), never more —
    only for candidates Mistral (or the keyword fallback) actually picked.
  - A brand is only ever attempted once: a brand_contacts row is created
    even when nothing is found, so it's never re-queried on a later run.

Phone numbers are NOT fetched: Apollo only delivers phone numbers
asynchronously via a webhook callback (reveal_phone_number requires a
webhook_url), which this pipeline has no infrastructure for yet. The
`phone` column exists on brand_contacts for when that's built later.
"""

import json
import logging
import time

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from config import APOLLO_API_KEY, MISTRAL_API_KEY
from pipeline.db import BrandContact, BrandProfile, BrandRaw, InitialBrandScore
from pipeline.helpers.llm import call_mistral_json

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
_MATCH_URL  = "https://api.apollo.io/v1/people/match"
_HEADERS    = {"Content-Type": "application/json", "X-Api-Key": APOLLO_API_KEY}
_TIMEOUT    = 20
_SEARCH_PER_PAGE = 25   # search is free — no credit reason to keep this small
_TOP_N = 5              # max contacts stored per brand — also caps enrich calls per brand

# Broad marketing-department sweep. Combined with include_similar_titles=True,
# this is the closest equivalent to "give me the whole marketing department"
# since Apollo's API has no confirmed department= input filter.
_MARKETING_DEPARTMENT_TITLES = [
    "marketing", "brand", "partnerships", "influencer",
    "social media", "communications", "growth", "content", "PR",
]

# Keyword fallback used only if MISTRAL_API_KEY isn't set, or Mistral's
# picks don't match any real candidate.
_TITLE_PRIORITIES: list[tuple[list[str], int]] = [
    (["partnership"], 1),
    (["sponsorship"], 1),
    (["influencer"], 2),
    (["brand marketing"], 2),
    (["social media"], 3),
    (["marketing manager"], 3),
    (["marketing director", "director of marketing"], 4),
    (["cmo", "chief marketing"], 5),
    (["marketing"], 6),
]


class _ApolloAuthError(Exception):
    """Raised when the Apollo key is invalid/expired — aborts the whole run."""


def _brand_location(brand: BrandRaw) -> str | None:
    return brand.headquarters or brand.location or brand.country or None


def _search_people(brand: BrandRaw) -> list[dict] | None:
    """Free Apollo search — no email/phone revealed, just candidate previews."""
    payload = {
        "person_titles":           _MARKETING_DEPARTMENT_TITLES,
        "include_similar_titles":  True,
        "per_page":                _SEARCH_PER_PAGE,
    }
    if brand.domain:
        payload["q_organization_domains_list"] = [brand.domain]
    else:
        payload["organization_names"] = [brand.name]

    location = _brand_location(brand)
    if location:
        payload["person_locations"] = [location]

    try:
        resp = httpx.post(_SEARCH_URL, headers=_HEADERS, json=payload, timeout=_TIMEOUT)
        if resp.status_code in (401, 403):
            raise _ApolloAuthError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code == 429:
            logger.warning("Apollo search: rate limited (429) — waiting 30s")
            time.sleep(30)
            resp = httpx.post(_SEARCH_URL, headers=_HEADERS, json=payload, timeout=_TIMEOUT)
        if resp.status_code >= 400:
            logger.warning("Apollo search failed for '%s': HTTP %s — %s", brand.name, resp.status_code, resp.text[:200])
            return []
        return resp.json().get("people", [])
    except _ApolloAuthError:
        raise
    except Exception:
        logger.exception("Apollo search request failed for '%s'", brand.name)
        return []


def _score_title(title: str) -> int:
    t = (title or "").lower()
    for keywords, score in _TITLE_PRIORITIES:
        if any(kw in t for kw in keywords):
            return score
    return 99


def _keyword_pick_top(people: list[dict], top_n: int = _TOP_N) -> list[tuple[dict, str]]:
    """Fallback when Mistral isn't available (or its picks don't match) — same keyword scoring as before."""
    if not people:
        return []
    ranked = sorted(people, key=lambda p: _score_title(p.get("title", "")))[:top_n]
    return [(p, f"keyword match on title '{p.get('title', '')}'") for p in ranked]


def _llm_pick_top_candidates(people: list[dict], brand_name: str, top_n: int = _TOP_N) -> list[tuple[dict, str]]:
    """
    Ask Mistral to rank candidates and pick up to top_n people most likely to
    personally own sponsorship/influencer-marketing budget decisions.
    Returns a best-first list of (person_dict, reason). Empty list if
    Mistral judges nobody a good fit; falls back to keyword scoring if
    Mistral is unavailable or its picks don't match any real candidate.
    """
    if not MISTRAL_API_KEY:
        return _keyword_pick_top(people, top_n)
    if not people:
        return []

    candidates = [
        {
            "id":               p.get("id"),
            "name":             (f"{p.get('first_name', '')} {p.get('last_name_obfuscated') or p.get('last_name', '')}").strip(),
            "title":            p.get("title", ""),
            "has_email":        bool(p.get("has_email")),
            "has_direct_phone": p.get("has_direct_phone"),
        }
        for p in people
    ]

    prompt = f"""You are a sponsorship-outreach research assistant. A content creator (Instagram/YouTube) wants to find the best people at "{brand_name}" to pitch for a paid sponsorship or influencer-marketing partnership.

You will receive a JSON list of employees at the brand (id, name, job title, and whether Apollo has an email/phone on file). Rank them by how likely each person is to personally own or directly influence influencer/sponsorship/partnership budget decisions.

Strongly prefer titles such as: Influencer Marketing Manager, Partnerships Manager, Brand Partnerships, Social Media Manager, Brand Marketing Manager, Community Manager, Growth Marketing.
Deprioritize unrelated departments (engineering, finance, legal, HR, sales-only roles) and very senior C-suite/VP titles who rarely personally answer inbound sponsorship pitches, unless no better option exists in the list.
All else equal, prefer candidates with has_email=true.

Candidates:
{json.dumps(candidates, indent=2)}

Pick up to {top_n} candidates, best first. Reply ONLY with this JSON object:
{{"picks": [{{"id": "...", "reason": "short one-line reason"}}, ...]}}
List best match first. Include at most {top_n} picks. If fewer than that many are plausible, return fewer — do not pad with irrelevant people. If none are a reasonable fit, reply with {{"picks": []}}."""

    result = call_mistral_json(prompt, context=f"apollo picks for {brand_name}")
    picks = result.get("picks", []) if isinstance(result, dict) else []

    by_id = {p.get("id"): p for p in people}
    ranked: list[tuple[dict, str]] = []
    for pick in picks[:top_n]:
        pid = pick.get("id")
        person = by_id.get(pid)
        if not person:
            logger.warning("Mistral picked id=%s which isn't in the candidate list — skipping", pid)
            continue
        ranked.append((person, pick.get("reason", "")))

    if not ranked:
        logger.info("Mistral returned no usable picks for '%s' — falling back to keyword scoring", brand_name)
        return _keyword_pick_top(people, top_n)

    return ranked


def _enrich_person(person_id: str) -> dict | None:
    """
    The expensive call — reveals email for one person.

    reveal_phone_number is deliberately NOT requested — Apollo only delivers
    phone numbers asynchronously via a webhook callback, which this pipeline
    doesn't have infrastructure for yet. Requesting it without a webhook_url
    makes the whole call fail with a 400.
    """
    payload = {"id": person_id, "reveal_personal_emails": True}
    try:
        resp = httpx.post(_MATCH_URL, headers=_HEADERS, json=payload, timeout=_TIMEOUT)
        if resp.status_code in (401, 403):
            raise _ApolloAuthError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code == 429:
            logger.warning("Apollo enrich: rate limited (429) — waiting 30s")
            time.sleep(30)
            resp = httpx.post(_MATCH_URL, headers=_HEADERS, json=payload, timeout=_TIMEOUT)
        if resp.status_code >= 400:
            logger.warning("Apollo enrich failed for person_id=%s: HTTP %s — %s", person_id, resp.status_code, resp.text[:200])
            return None
        return resp.json().get("person")
    except _ApolloAuthError:
        raise
    except Exception:
        logger.exception("Apollo enrich request failed for person_id=%s", person_id)
        return None


def _insert_contacts(db: Session, rows: list[dict]) -> None:
    """Bulk insert brand_contacts rows, skipping exact (brand, person) duplicates."""
    if not rows:
        return
    stmt = pg_insert(BrandContact).values(rows).on_conflict_do_nothing(
        index_elements=["brand_raw_id", "apollo_person_id"]
    )
    db.execute(stmt)
    db.commit()


def _insert_empty_marker(db: Session, brand_raw_id: int) -> None:
    """Marks a brand as attempted with zero usable contacts found."""
    stmt = pg_insert(BrandContact).values(brand_raw_id=brand_raw_id).on_conflict_do_nothing(
        index_elements=["brand_raw_id", "apollo_person_id"]
    )
    db.execute(stmt)
    db.commit()


def _upsert_profile_contact_fields(db: Session, brand_raw_id: int, values: dict) -> None:
    stmt = (
        pg_insert(BrandProfile)
        .values(brand_raw_id=brand_raw_id, **values)
        .on_conflict_do_update(index_elements=["brand_raw_id"], set_=values)
    )
    db.execute(stmt)
    db.commit()


def find_brand_contact(db: Session, brand_raw_id: int) -> list[dict]:
    """
    Find and store up to TOP_N best marketing contacts for one brand, ranked
    best-first. Always writes at least an empty marker row so the brand is
    never re-queried. Returns the list of stored contact dicts (rank 1
    first), or [] if none were found. Returns [] if the brand doesn't exist.
    """
    brand = db.query(BrandRaw).filter(BrandRaw.id == brand_raw_id).first()
    if not brand:
        logger.warning("Apollo contact: brand_raw_id=%d not found", brand_raw_id)
        return []

    people = _search_people(brand)

    if not people:
        logger.info("Apollo contact: '%s' — no marketing contacts found", brand.name)
        _insert_empty_marker(db, brand_raw_id)
        _upsert_profile_contact_fields(db, brand_raw_id, {
            "has_marketing_contact": False,
            "contact_mode": "outsourced_likely",
        })
        return []

    picks = _llm_pick_top_candidates(people, brand.name, top_n=_TOP_N)
    if not picks:
        logger.info("Apollo contact: '%s' — no candidate judged a good fit", brand.name)
        _insert_empty_marker(db, brand_raw_id)
        _upsert_profile_contact_fields(db, brand_raw_id, {
            "has_marketing_contact": False,
            "contact_mode": "outsourced_likely",
        })
        return []

    rows: list[dict] = []
    for rank, (candidate, reason) in enumerate(picks, start=1):
        enriched = _enrich_person(candidate["id"])
        if not enriched:
            logger.warning("Apollo contact: '%s' — enrich failed for rank %d, saving search data only", brand.name, rank)
            enriched = candidate

        first = enriched.get("first_name", "")
        last  = enriched.get("last_name", "")
        full_name = f"{first} {last}".strip() or enriched.get("name") or candidate.get("name")

        rows.append({
            "brand_raw_id":     brand_raw_id,
            "rank":             rank,
            "full_name":        full_name,
            "title":            enriched.get("title") or candidate.get("title"),
            "departments":      "; ".join(enriched.get("departments") or []) or None,
            "subdepartments":   "; ".join(enriched.get("subdepartments") or []) or None,
            "functions":        "; ".join(enriched.get("functions") or []) or None,
            "seniority":        enriched.get("seniority"),
            "email":            enriched.get("email"),
            "email_status":     enriched.get("email_status"),
            "phone":            None,   # see module docstring — needs webhook infra
            "linkedin_url":     enriched.get("linkedin_url") or candidate.get("linkedin_url"),
            "city":             enriched.get("city"),
            "state":            enriched.get("state"),
            "country":          enriched.get("country"),
            "llm_reason":       reason,
            "apollo_person_id": candidate.get("id"),
        })
        time.sleep(0.3)

    _insert_contacts(db, rows)
    top = rows[0]
    _upsert_profile_contact_fields(db, brand_raw_id, {
        "has_marketing_contact":    True,
        "contact_mode":             "in_house",
        "best_contact_title_score": 1,   # rank of the routing-signal contact — always the top pick
    })

    logger.info(
        "Apollo contact: '%s' -> %d contact(s) stored, top pick: %s (%s)",
        brand.name, len(rows), top["full_name"], top["title"],
    )
    return rows


def run_apollo_contacts(db: Session, limit: int = 20, brand_id: int | None = None) -> int:
    """
    Find Apollo contacts for brands scored >= 50 in initial_brand_score that
    haven't been attempted yet (no brand_contacts row at all). Pass brand_id
    to target one specific brand directly (bypasses the score filter and the
    already-attempted check, for testing).

    limit defaults small (20) since each brand costs up to TOP_N Apollo
    enrichment credits — raise it deliberately, don't crank it up by default.

    Returns number of brands processed.
    """
    if not APOLLO_API_KEY:
        logger.warning("APOLLO_API_KEY not set — skipping Apollo contact enrichment")
        return 0

    query = db.query(BrandRaw.id).join(InitialBrandScore, InitialBrandScore.brand_raw_id == BrandRaw.id)
    if brand_id is not None:
        query = query.filter(BrandRaw.id == brand_id)
    else:
        query = (
            query.filter(InitialBrandScore.total_score >= 50)
            .outerjoin(BrandContact, BrandContact.brand_raw_id == BrandRaw.id)
            .filter(BrandContact.id.is_(None))
        )

    brand_ids = [row.id for row in query.limit(limit).all()]

    if not brand_ids:
        logger.info("Apollo contacts: no qualifying brands to process")
        return 0

    logger.info("Apollo contacts: processing %d brands (limit=%d)", len(brand_ids), limit)
    processed = 0
    for bid in brand_ids:
        try:
            find_brand_contact(db, bid)
            processed += 1
        except _ApolloAuthError as exc:
            logger.error(
                "Apollo contacts: auth error — aborting run, remaining brands untouched. "
                "Check APOLLO_API_KEY. (%s)", exc,
            )
            break
        time.sleep(0.5)

    logger.info("Apollo contacts: %d/%d brands processed", processed, len(brand_ids))
    return processed
