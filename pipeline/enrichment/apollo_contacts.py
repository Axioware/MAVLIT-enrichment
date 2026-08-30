"""
pipeline/enrichment/apollo_contacts.py

Finds up to 50 marketing/sponsorship contacts for high-scoring brands,
ranks ALL of them best-first via OpenAI, and stores the full ranked list
in brand_contacts — gives a content creator a whole queue of people to try,
not just one. Only the top 5 are enriched (the paid Apollo call that
reveals a real email); the rest are stored from the free search preview
only. Also updates brand_match_profile's contact routing fields
(has_marketing_contact, contact_mode, best_contact_title_score) per the
matching design doc, based on the top-ranked (rank=1) contact.

Runs only for brands in initial_brand_score with total_score >= 50.

Pipeline (adapted from apollo_sponsorship_finder.py, minus the CSV/testing bits):
  1) SEARCH    -> Apollo /api/v1/mixed_people/api_search. FREE (0 credits).
                  Sweeps the whole marketing department via a broad title
                  list + include_similar_titles=True — Apollo has no
                  confirmed person_departments= input filter (department
                  only comes back as an OUTPUT field after enrichment), so
                  a title-keyword sweep is the closest equivalent to "all
                  of marketing".
  1b) FALLBACK -> If the marketing sweep finds nobody at all, that usually
                  means sponsorship/influencer outreach is outsourced to an
                  agency — there's no in-house marketing contact to route
                  through. In that case, search company leadership instead
                  (person_seniorities=c_suite/founder/owner, no title
                  filter) so the creator still has someone to reach out to
                  directly. Still free.
  2) RANK      -> OpenAI (pipeline.helpers.gpt_llm.call_gpt_json) ranks
                  ALL of the found candidates (up to 50) best-first by how
                  likely each is to personally own or influence
                  sponsorship/influencer-marketing decisions. Any candidate
                  the response omits is appended at the end (in
                  original order) so nobody found in search is ever
                  silently dropped. Costs a fraction of a cent of OpenAI
                  usage, not Apollo credits. Falls back to keyword/order
                  ranking if OPENAI_KEY isn't set, or if the LLM's
                  response is unusable.
  3) ENRICH    -> Apollo /v1/people/match. Only the top ENRICH_TOP_N-ranked
                  candidates get enriched to reveal a real name/email —
                  this is the only step that costs Apollo credits, and it
                  only ever runs once per person (up to ENRICH_TOP_N
                  credits per brand, never more, regardless of how many
                  candidates were found/ranked/stored). Of those,
                  only the top PHONE_TOP_N also get phone reveal (see
                  below) — a separate, additional cost per person.

Credit-conscious by design:
  - Search is free — no need to ration it.
  - At most ENRICH_TOP_N Apollo email-enrich calls per brand (default 5),
    never more, no matter how many of the up-to-50 ranked candidates get
    stored.
  - A brand is only ever attempted once: a brand_contacts row is created
    even when nothing is found, so it's never re-queried on a later run.
  - Phone reveal (see below) is a SEPARATE, additional cost on top of the
    email enrich call — confirmed live at 8 Apollo credits per person.
    Bounded independently of ENRICH_TOP_N via _PHONE_TOP_N (default 2):
    only rank <= _PHONE_TOP_N gets phone reveal, so raising ENRICH_TOP_N
    (more emails) never silently raises phone cost too. Only runs at all
    when ENABLE_APOLLO_PHONE_REVEAL=true (config.py); leave it unset/false
    to skip phone reveal entirely regardless of _PHONE_TOP_N.

Phone numbers: reveal_phone_number resolves asynchronously on Apollo's
side. The /v1/people/match response for it is never a phone number
directly — it's {"phone_enrichment": {"status": "pending", ...},
"request_id": ...}. The reliable way to get the actual number (confirmed
against live Apollo responses) is polling GET /api/v1/webhook_result/
{request_id} every _PHONE_POLL_INTERVAL seconds, up to _PHONE_POLL_ATTEMPTS
times. Apollo's docs describe a webhook_url push callback instead, and
requesting reveal_phone_number without a syntactically valid webhook_url
in the payload still 400s — but in practice the push delivery isn't
reliable (confirmed: a request to a deliberately unreachable dummy
webhook_url still resolved successfully via polling), so a fixed
placeholder webhook_url is sent purely to satisfy that required-field
validation; polling is the only mechanism this code actually depends on.
No public URL/tunnel of our own is needed for this to work.
"""
    
import json
import logging
import time

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from config import APOLLO_API_KEY, ENABLE_APOLLO_PHONE_REVEAL, OPENAI_KEY
from pipeline.db import BrandContact, BrandProfile, BrandRaw, InitialBrandScore, Prompt
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template
from pipeline.helpers.prompts import APOLLO_RANK_PROMPT_NAME, APOLLO_RANK_DEFAULT_PROMPT

logger = logging.getLogger(__name__)

_SEARCH_URL        = "https://api.apollo.io/api/v1/mixed_people/api_search"
_MATCH_URL         = "https://api.apollo.io/v1/people/match"
_WEBHOOK_RESULT_URL = "https://api.apollo.io/api/v1/webhook_result/{}"
# reveal_phone_number requires a syntactically valid webhook_url in the
# payload or Apollo 400s — it doesn't need to be reachable (confirmed live),
# since this code polls for the result instead of waiting on a push.
_PHONE_WEBHOOK_PLACEHOLDER = "https://example.com/apollo-phone-webhook-unused"
_HEADERS    = {"Content-Type": "application/json", "X-Api-Key": APOLLO_API_KEY}
_TIMEOUT    = 20
_SEARCH_PER_PAGE = 50   # search is free — no credit reason to keep this small
_ENRICH_TOP_N = 5       # only this many ranked candidates get the paid email enrich call
_PHONE_TOP_N = 2        # of those, only this many (rank <= this) also get phone reveal — extra ~8 credits each

# Phone reveal is async — see module docstring. Apollo's own guidance is
# "retry after ~10 seconds"; this polls up to 7 times (~70s worst case per
# person) before giving up and leaving phone NULL for that person.
_PHONE_POLL_INTERVAL = 10
_PHONE_POLL_ATTEMPTS = 7

# Broad marketing-department sweep. Combined with include_similar_titles=True,
# this is the closest equivalent to "give me the whole marketing department"
# since Apollo's API has no confirmed department= input filter. The first
# four terms are sponsorship-outreach-specific signal kept from an earlier,
# narrower list; the rest is Apollo's own marketing sub-department taxonomy.
_MARKETING_DEPARTMENT_TITLES = [
    "influencer", "partnerships", "sponsorship", "growth",
    "advertising",
    "brand management",
    "content marketing",
    "customer experience",
    "customer marketing",
    "demand generation",
    "digital marketing",
    "ecommerce marketing",
    "event marketing",
    "field marketing",
    "lead generation",
    "marketing",
    "marketing analytics",
    "marketing insights",
    "marketing communications",
    "marketing operations",
    "product marketing",
    "public relations",
    "search engine optimization",
    "pay per click",
    "social media marketing",
    "strategic communications",
    "technical marketing",
]

# Fallback search when no marketing-titled person is found at all — go
# straight to company leadership instead of giving up.
_C_SUITE_SENIORITIES = ["c_suite", "founder", "owner"]

# Keyword fallback used only if OPENAI_KEY isn't set, or the LLM's
# response is unusable, for the marketing-search path.
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


def _apollo_search(payload: dict, brand_name: str) -> list[dict]:
    """Shared free-search POST with 429 retry + auth-error handling."""
    try:
        resp = httpx.post(_SEARCH_URL, headers=_HEADERS, json=payload, timeout=_TIMEOUT)
        if resp.status_code in (401, 403):
            raise _ApolloAuthError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        if resp.status_code == 429:
            logger.warning("Apollo search: rate limited (429) — waiting 30s")
            time.sleep(30)
            resp = httpx.post(_SEARCH_URL, headers=_HEADERS, json=payload, timeout=_TIMEOUT)
        if resp.status_code >= 400:
            logger.warning("Apollo search failed for '%s': HTTP %s — %s", brand_name, resp.status_code, resp.text[:200])
            return []
        return resp.json().get("people", [])
    except _ApolloAuthError:
        raise
    except Exception:
        logger.exception("Apollo search request failed for '%s'", brand_name)
        return []


def _search_people(brand: BrandRaw) -> list[dict]:
    """Free Apollo search across the marketing department — no email/phone revealed."""
    payload = {
        "person_titles":          _MARKETING_DEPARTMENT_TITLES,
        "include_similar_titles": True,
        "per_page":               _SEARCH_PER_PAGE,
    }
    if brand.domain:
        payload["q_organization_domains_list"] = [brand.domain]
    else:
        payload["organization_names"] = [brand.name]

    location = _brand_location(brand)
    if location:
        payload["person_locations"] = [location]

    return _apollo_search(payload, brand.name)


def _search_c_suite(brand: BrandRaw) -> list[dict]:
    """
    Fallback when no marketing-department person is found at all — likely
    means sponsorship/influencer outreach is handled by an outside agency,
    so there's no marketing contact to route through. Goes straight to
    company leadership instead so the creator still has someone to pitch.
    """
    payload = {
        "person_seniorities": _C_SUITE_SENIORITIES,
        "per_page":           _SEARCH_PER_PAGE,
    }
    if brand.domain:
        payload["q_organization_domains_list"] = [brand.domain]
    else:
        payload["organization_names"] = [brand.name]

    location = _brand_location(brand)
    if location:
        payload["person_locations"] = [location]

    return _apollo_search(payload, brand.name)


def _score_title(title: str) -> int:
    t = (title or "").lower()
    for keywords, score in _TITLE_PRIORITIES:
        if any(kw in t for kw in keywords):
            return score
    return 99


def _keyword_rank_all(people: list[dict], fallback_mode: bool) -> list[tuple[dict, str]]:
    """
    Fallback ranking used only if OPENAI_KEY isn't set, or the LLM's
    response is unusable. Marketing-title keyword scoring doesn't apply to
    the C-suite fallback pool (everyone there is already an executive), so
    that case just keeps Apollo's own result order.
    """
    if not people:
        return []
    if fallback_mode:
        return [(p, "C-suite fallback — Apollo result order (no LLM ranking available)") for p in people]
    ranked = sorted(people, key=lambda p: _score_title(p.get("title", "")))
    return [(p, f"keyword match on title '{p.get('title', '')}'") for p in ranked]


#  Prompt helpers

def _get_apollo_rank_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == APOLLO_RANK_PROMPT_NAME).first()
    return row.content if row else APOLLO_RANK_DEFAULT_PROMPT


def _rank_all_candidates(db: Session, people: list[dict], brand_name: str, fallback_mode: bool = False) -> list[tuple[dict, str]]:
    """
    Ask OpenAI to rank ALL found candidates (up to 50) best-first by how
    likely each is to personally own or influence sponsorship/influencer-
    marketing decisions. Returns a best-first list of (person_dict, reason)
    covering every candidate in `people` — any candidate the response
    doesn't explicitly rank is appended at the end (original order) so
    nobody is ever silently dropped from storage. Falls back to keyword/
    order ranking if OPENAI_KEY isn't set or the response is
    unusable.
    """
    if not people:
        return []
    if not OPENAI_KEY:
        return _keyword_rank_all(people, fallback_mode)

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

    if fallback_mode:
        intro = (
            f'No dedicated marketing/influencer contact was found at "{brand_name}" — this usually means '
            "sponsorship or influencer outreach is handled by an outside agency rather than in-house. "
            "Since there is no marketing team to route through, these are COMPANY LEADERSHIP (C-suite/"
            "founder/owner) contacts instead. Rank them by how likely each is to personally consider or "
            "forward a content-creator sponsorship pitch — prefer marketing/growth/brand-oriented "
            "executives (CMO, Chief Growth Officer, Chief Brand Officer, Founder/CEO of a small company) "
            "over finance/engineering/legal-focused ones (CFO, CTO, General Counsel)."
        )
        title_hint = ""
    else:
        intro = (
            f'A content creator (Instagram/YouTube) wants to find the best people at "{brand_name}" to pitch '
            "for a paid sponsorship or influencer-marketing partnership."
        )
        title_hint = (
            "\nStrongly prefer titles such as: Influencer Marketing Manager, Partnerships Manager, Brand "
            "Partnerships, Social Media Manager, Brand Marketing Manager, Community Manager, Growth "
            "Marketing. Deprioritize very senior C-suite/VP titles who rarely personally answer inbound "
            "sponsorship pitches, unless no better option exists in the list."
        )

    prompt = fill_template(
        _get_apollo_rank_prompt(db),
        intro=intro,
        title_hint=title_hint,
        candidates=json.dumps(candidates, indent=2),
    )

    result = call_gpt_json(prompt, context=f"apollo full ranking for {brand_name}")
    picks = result.get("picks", []) if isinstance(result, dict) else []

    by_id = {p.get("id"): p for p in people}
    ranked: list[tuple[dict, str]] = []
    seen_ids: set = set()
    for pick in picks:
        pid = pick.get("id")
        if pid in seen_ids:
            continue
        person = by_id.get(pid)
        if not person:
            logger.warning("LLM picked id=%s which isn't in the candidate list — skipping", pid)
            continue
        seen_ids.add(pid)
        ranked.append((person, pick.get("reason", "")))

    # Safety net: never let a candidate found in search go unstored just
    # because the LLM's response omitted it.
    for p in people:
        pid = p.get("id")
        if pid not in seen_ids:
            ranked.append((p, "not explicitly ranked by LLM"))
            seen_ids.add(pid)

    if not ranked:
        logger.info("LLM returned no usable ranking for '%s' — falling back to keyword/order ranking", brand_name)
        return _keyword_rank_all(people, fallback_mode)

    return ranked


def _extract_phone_from_person(person: dict) -> str | None:
    numbers = person.get("phone_numbers")
    if isinstance(numbers, list) and numbers:
        first = numbers[0]
        if isinstance(first, dict):
            return first.get("sanitized_number") or first.get("raw_number")
    return None


def _poll_phone_result(request_id, person_id: str) -> str | None:
    """
    reveal_phone_number resolves asynchronously — see module docstring for
    why polling, not the webhook_url push, is what this depends on. Blocks
    up to _PHONE_POLL_ATTEMPTS * _PHONE_POLL_INTERVAL seconds; gives up
    (returns None, phone stays NULL) if it's still not ready by then.
    """
    url = _WEBHOOK_RESULT_URL.format(request_id)
    for attempt in range(_PHONE_POLL_ATTEMPTS):
        time.sleep(_PHONE_POLL_INTERVAL)
        try:
            resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT)
        except Exception:
            logger.exception("Apollo phone poll request failed for person_id=%s", person_id)
            return None
        if resp.status_code == 404:
            continue   # not ready yet
        if resp.status_code >= 400:
            logger.warning("Apollo phone poll HTTP %s for person_id=%s: %s", resp.status_code, person_id, resp.text[:200])
            return None
        data = resp.json()
        people = (data.get("webhook_result") or {}).get("people") or []
        if not people:
            continue
        return _extract_phone_from_person(people[0])
    logger.info("Apollo phone poll: person_id=%s still pending after %d attempts (~%ds) — giving up, phone stays NULL",
                person_id, _PHONE_POLL_ATTEMPTS, _PHONE_POLL_ATTEMPTS * _PHONE_POLL_INTERVAL)
    return None


def _enrich_person(person_id: str, reveal_phone: bool = False) -> dict | None:
    """
    The expensive call — reveals email for one person, and (only when both
    `reveal_phone` is True and ENABLE_APOLLO_PHONE_REVEAL is set) polls for
    a phone number too — an extra ~8 Apollo credits and up to ~70s of
    blocking per person. See module docstring for why this polls rather
    than waiting on a webhook push. Callers pass reveal_phone=True only for
    rank <= _PHONE_TOP_N, so phone cost stays bounded independent of how
    many people get email-enriched.
    """
    payload = {"id": person_id, "reveal_personal_emails": True}
    if reveal_phone and ENABLE_APOLLO_PHONE_REVEAL:
        payload["reveal_phone_number"] = True
        payload["webhook_url"] = _PHONE_WEBHOOK_PLACEHOLDER
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

        data = resp.json()
        person = data.get("person")
        if not person:
            return None

        phone = _extract_phone_from_person(person)
        phone_enrichment = data.get("phone_enrichment")
        if not phone and phone_enrichment and phone_enrichment.get("status") == "pending":
            request_id = data.get("request_id")
            if request_id is not None:
                phone = _poll_phone_result(request_id, person_id)
        if phone:
            person["phone"] = phone
        return person
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
    Find and store up to 50 ranked marketing/sponsorship contacts for one
    brand, best-first. Only the top ENRICH_TOP_N get the paid Apollo enrich
    call (real name/email); the rest are stored from free search-preview
    data only. Always writes at least an empty marker row so the brand is
    never re-queried. Returns the list of stored contact dicts (rank 1
    first), or [] if none were found. Returns [] if the brand doesn't exist.
    """
    brand = db.query(BrandRaw).filter(BrandRaw.id == brand_raw_id).first()
    if not brand:
        logger.warning("Apollo contact: brand_raw_id=%d not found", brand_raw_id)
        return []

    people = _search_people(brand)
    fallback_used = False

    if not people:
        logger.info("Apollo contact: '%s' — no marketing contacts found, trying C-suite fallback", brand.name)
        people = _search_c_suite(brand)
        fallback_used = True

    if not people:
        logger.info("Apollo contact: '%s' — no contacts found at all (marketing or C-suite)", brand.name)
        _insert_empty_marker(db, brand_raw_id)
        _upsert_profile_contact_fields(db, brand_raw_id, {
            "has_marketing_contact": False,
            "contact_mode": "none",
        })
        return []

    picks = _rank_all_candidates(db, people, brand.name, fallback_mode=fallback_used)
    if not picks:
        logger.info("Apollo contact: '%s' — ranking returned nothing usable", brand.name)
        _insert_empty_marker(db, brand_raw_id)
        _upsert_profile_contact_fields(db, brand_raw_id, {
            "has_marketing_contact": False,
            "contact_mode": "none",
        })
        return []

    rows: list[dict] = []
    for rank, (candidate, reason) in enumerate(picks, start=1):
        enriched = None
        if rank <= _ENRICH_TOP_N:
            enriched = _enrich_person(candidate["id"], reveal_phone=(rank <= _PHONE_TOP_N))
            if not enriched:
                logger.warning("Apollo contact: '%s' — enrich failed for rank %d, saving search data only", brand.name, rank)
            time.sleep(0.3)

        source = enriched or candidate
        first = source.get("first_name", "")
        last  = source.get("last_name") or source.get("last_name_obfuscated", "")
        full_name = f"{first} {last}".strip() or source.get("name") or candidate.get("name")

        rows.append({
            "brand_raw_id":     brand_raw_id,
            "rank":             rank,
            "is_enriched":      bool(enriched),
            "full_name":        full_name,
            "title":            source.get("title") or candidate.get("title"),
            "departments":      "; ".join(source.get("departments") or []) or None,
            "subdepartments":   "; ".join(source.get("subdepartments") or []) or None,
            "functions":        "; ".join(source.get("functions") or []) or None,
            "seniority":        source.get("seniority"),
            "email":            source.get("email"),
            "email_status":     source.get("email_status"),
            "phone":            source.get("phone"),   # None unless ENABLE_APOLLO_PHONE_REVEAL is set and the poll succeeded
            "linkedin_url":     source.get("linkedin_url") or candidate.get("linkedin_url"),
            "city":             source.get("city"),
            "state":            source.get("state"),
            "country":          source.get("country"),
            "llm_reason":       reason,
            "apollo_person_id": candidate.get("id"),
        })

    _insert_contacts(db, rows)
    top = rows[0]
    _upsert_profile_contact_fields(db, brand_raw_id, {
        "has_marketing_contact":    not fallback_used,   # True only for a genuine marketing-titled hit
        "contact_mode":             "outsourced_likely" if fallback_used else "in_house",
        "best_contact_title_score": 1,   # rank of the routing-signal contact — always the top pick
    })

    logger.info(
        "Apollo contact: '%s' -> %d contact(s) stored (%d enriched), top pick: %s (%s)%s",
        brand.name, len(rows), min(len(rows), _ENRICH_TOP_N), top["full_name"], top["title"],
        " [C-suite fallback]" if fallback_used else "",
    )
    return rows


def run_apollo_contacts(db: Session, limit: int = 20, brand_id: int | None = None) -> int:
    """
    Find Apollo contacts for brands scored >= 50 in initial_brand_score that
    haven't been attempted yet (no brand_contacts row at all). Pass brand_id
    to target one specific brand directly (bypasses the score filter and the
    already-attempted check, for testing).

    limit defaults small (20) since each brand costs up to ENRICH_TOP_N
    Apollo enrichment credits — raise it deliberately, don't crank it up by
    default.

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
            query.filter(
                InitialBrandScore.total_score >= 50,
                BrandRaw.has_official_website == True,
            )
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
