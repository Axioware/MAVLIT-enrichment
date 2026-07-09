#!/usr/bin/env python3
"""
apollo_sponsorship_finder.py

Finds the best sponsorship / influencer-marketing contacts at a brand,
using the Apollo.io API for data and Mistral for ranking.

Target brand is set in the CONFIG section below (currently: BetterHelp).
Just run:

    python apollo_sponsorship_finder.py

PIPELINE
--------
  1) SEARCH   -> Apollo /api/v1/mixed_people/api_search
                 FREE (0 credits). Sweeps the whole marketing department
                 (marketing, brand, partnerships, influencer, social media,
                 growth, content, PR) via include_similar_titles=true.
                 Returns an obfuscated preview: title, org, has_email flag,
                 etc. — no real email/phone yet. Saved to CSV (all columns).

  2) RANK     -> Mistral chat completions (MISTRAL_API_KEY)
                 Sends the candidate list (title, org, has_email) and asks
                 Mistral to pick the TOP_N people most likely to personally
                 own sponsorship/influencer budget decisions. Costs a few
                 cents of Mistral usage (~$0.10/1M input tokens on
                 mistral-small-latest) — NOT Apollo credits.

  3) ENRICH   -> Apollo /api/v1/people/match
                 Only Mistral's TOP_N picks get enriched to reveal real
                 name/email/LinkedIn. Costs up to TOP_N Apollo credits —
                 nothing is wasted enriching people who aren't a fit.

CONFIG
------
Edit the CONFIG section below to target a different brand: BRAND_NAME,
DOMAIN, LOCATION, TOP_N.

SETUP
-----
pip install requests
export APOLLO_API_KEY="your_apollo_key"
export MISTRAL_API_KEY="your_mistral_key"
"""

import csv
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional
from config import APOLLO_API_KEY, MISTRAL_API_KEY
import requests

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------

API_KEY = APOLLO_API_KEY
BASE_URL = "https://api.apollo.io/api/v1"

MISTRAL_API_KEY = MISTRAL_API_KEY
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-small-latest")  # cheap: ~$0.10/1M input tokens
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"

# Titles most likely to own sponsorship / influencer-marketing / partnerships
# budget at a brand. Ordered roughly most-relevant-first; used as the default
# enrichment priority order too.
DEFAULT_TITLES = [
    "Influencer Marketing Manager",
    "Influencer Marketing",
    "Influencer Marketing Lead",
    "Partnerships Manager",
    "Brand Partnerships Manager",
    "Brand Partnerships",
    "Partnerships Lead",
    "Head of Partnerships",
    "Social Media Manager",
    "Brand Manager",
    "Digital Marketing Manager",
    "Growth Marketing Manager",
    "Marketing Manager",
    "Community Manager",
    "Marketing Director",
    "Head of Marketing",
    "VP Marketing",
    "Chief Marketing Officer",
    "CMO",
]

HEADERS = {
    "x-api-key": API_KEY,
    "Content-Type": "application/json",
    "accept": "application/json",
}

# Confirmed valid values for person_seniorities (from Apollo docs):
# owner, founder, c_suite, partner, vp, head, director, manager, senior, entry, intern
SPONSORSHIP_SENIORITIES = ["c_suite", "vp", "head", "director", "manager"]

# Broad marketing-department sweep. Combined with include_similar_titles=True
# in search_people(), this is the closest equivalent to "give me the whole
# marketing department" since Apollo's public API has no confirmed
# department= filter — only title/seniority matching.
MARKETING_DEPARTMENT_TITLES = [
    "marketing",
    "brand",
    "partnerships",
    "influencer",
    "social media",
    "communications",
    "growth",
    "content",
    "PR",
]


# ----------------------------------------------------------------------------
# STEP 1: FREE SEARCH
# ----------------------------------------------------------------------------

def search_people(
    domain: Optional[str] = None,
    brand_name: Optional[str] = None,
    location: Optional[str] = None,
    titles: Optional[List[str]] = None,
    include_similar_titles: bool = True,
    seniorities: Optional[List[str]] = None,
    per_page: int = 25,
    max_pages: int = 2,
) -> List[Dict[str, Any]]:
    """
    Calls Apollo's People API Search (free, no credits consumed).
    Returns a list of raw person dicts as Apollo gives them.

    include_similar_titles=True makes Apollo also match adjacent/related
    titles (Apollo's closest equivalent to a "whole department" filter,
    since there's no confirmed public `department=` param). e.g. searching
    "marketing" also surfaces "Head of Brand Marketing", "Growth Marketing
    Lead", etc.
    """
    titles = titles or DEFAULT_TITLES
    url = f"{BASE_URL}/mixed_people/api_search"

    all_people: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        payload: Dict[str, Any] = {
            "person_titles": titles,
            "include_similar_titles": include_similar_titles,
            "per_page": per_page,
            "page": page,
        }
        if seniorities:
            payload["person_seniorities"] = seniorities
        # Domain is by far the most reliable way to pin the company down.
        # NOTE: the correct Apollo param is q_organization_domains_list
        # (NOT q_organization_domains, which Apollo silently ignores).
        if domain:
            payload["q_organization_domains_list"] = [domain]
        elif brand_name:
            # Fallback if you don't have a domain — less precise, Apollo will
            # do a keyword match on company name.
            payload["organization_names"] = [brand_name]

        if location:
            payload["person_locations"] = [location]

        resp = requests.post(url, headers=HEADERS, json=payload, timeout=30)

        if resp.status_code == 401:
            sys.exit("Apollo returned 401 Unauthorized — check your API key.")
        if resp.status_code == 403:
            sys.exit(
                "Apollo returned 403 Forbidden. On some plans "
                "mixed_people/api_search is restricted — check your plan's "
                "API access in Apollo Settings > API."
            )
        if resp.status_code != 200:
            print(f"[warn] Apollo returned {resp.status_code}: {resp.text[:500]}")
            break

        data = resp.json()
        people = data.get("people") or []
        total_entries = data.get("total_entries", 0)

        if page == 1:
            print(f"[debug] total_entries reported by Apollo: {total_entries}")
            if total_entries == 0:
                print(
                    "[debug] Apollo returned 0 total_entries. Common causes:\n"
                    "  1) Your API key is not a 'master' key. People API Search "
                    "requires a master key (Apollo Settings > API > API Keys > "
                    "make sure it's created/marked as a master key).\n"
                    "  2) Filters are too narrow / combined with AND logic and "
                    "nothing matches all of them at once — try dropping "
                    "--location or shortening --titles to test.\n"
                    "  3) Wrong domain format (should be like 'nike.com', no "
                    "https:// or www.).\n"
                    f"[debug] Raw response (first 500 chars): {resp.text[:500]}"
                )

        if not people:
            break

        all_people.extend(people)

        pagination = data.get("pagination", {})
        total_pages = pagination.get("total_pages", 1)
        print(f"[search] page {page}/{total_pages} -> {len(people)} people")

        if page >= total_pages:
            break
        time.sleep(0.4)  # be polite to the API

    return all_people


# ----------------------------------------------------------------------------
# LLM RANKING (Mistral) — picks the best sponsorship contacts from the free
# search results, so we only spend Apollo credits enriching people worth it.
# ----------------------------------------------------------------------------

def llm_rank_candidates(
    people: List[Dict[str, Any]],
    brand: str,
    top_n: int = 5,
) -> List[Dict[str, Any]]:
    """
    Sends the free-search candidate list to Mistral and asks it to pick the
    top_n people most likely to control sponsorship / influencer-marketing
    budget for a content creator (Instagram/YouTube) reaching out to `brand`.

    Costs a few cents of Mistral usage (NOT Apollo credits) — this is what
    lets you enrich exactly top_n people instead of guessing/enriching more.

    Returns the subset of `people` dicts Mistral picked, in ranked order,
    each with an added "_llm_reason" field.
    """
    if not MISTRAL_API_KEY:
        sys.exit("Set MISTRAL_API_KEY env var to use --llm-select.")
    if not people:
        return []

    # Build a compact candidate list — only the fields the LLM needs to
    # judge relevance. Keeps the prompt cheap and avoids noise.
    candidates = []
    for p in people:
        flat = flatten(p)
        candidates.append({
            "id": p.get("id"),
            "first_name": flat.get("first_name", ""),
            "last_name": flat.get("last_name_obfuscated") or flat.get("last_name", ""),
            "title": flat.get("title", ""),
            "organization": flat.get("organization.name", ""),
            "has_email": bool(flat.get("has_email", False)),
            "has_direct_phone": flat.get("has_direct_phone", ""),
        })

    system_prompt = (
        "You are a sponsorship-outreach research assistant. A content creator "
        "(Instagram/YouTube) wants to find the best person(s) at a brand to "
        "pitch for paid sponsorship or an influencer-marketing partnership.\n\n"
        "You will receive a JSON list of employees at the brand (id, name, "
        "job title, and whether Apollo has an email/phone on file). Rank them "
        "by how likely each person is to personally own or directly influence "
        "influencer/sponsorship/partnership budget decisions.\n\n"
        "Strongly prefer titles such as: Influencer Marketing Manager, "
        "Partnerships Manager, Brand Partnerships, Social Media Manager, "
        "Brand Marketing Manager, Community Manager, Growth Marketing.\n"
        "Deprioritize unrelated departments (engineering, finance, legal, HR, "
        "sales-only roles) and very senior C-suite/VP titles who rarely "
        "personally answer inbound sponsorship pitches, UNLESS no better "
        "option exists in the list.\n"
        "All else equal, prefer candidates with has_email=true, since that "
        "increases the odds enrichment actually returns a usable email.\n\n"
        "Respond with ONLY a JSON object of the form:\n"
        '{"picks": [{"id": "...", "title": "...", "reason": "short one-line reason"}]}\n'
        "List best match first. Include at most the requested number of picks. "
        "If fewer than that many are plausible, return fewer — do not pad with "
        "irrelevant people."
    )

    user_prompt = (
        f"Brand: {brand}\n"
        f"Pick the top {top_n} candidates most likely to control sponsorship / "
        f"influencer-marketing budget, from this list:\n\n"
        f"{json.dumps(candidates, indent=2)}"
    )

    resp = requests.post(
        MISTRAL_URL,
        headers={
            "Authorization": f"Bearer {MISTRAL_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": MISTRAL_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )

    if resp.status_code != 200:
        sys.exit(f"Mistral API error {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    content = data["choices"][0]["message"]["content"]

    try:
        parsed = json.loads(content)
        picks = parsed.get("picks", [])
    except (json.JSONDecodeError, KeyError, AttributeError) as e:
        sys.exit(f"Couldn't parse Mistral's response as JSON: {e}\nRaw content: {content[:500]}")

    by_id = {p.get("id"): p for p in people}
    ranked: List[Dict[str, Any]] = []
    for pick in picks[:top_n]:
        pid = pick.get("id")
        person = by_id.get(pid)
        if person is None:
            print(f"[warn] Mistral picked id={pid} which isn't in the candidate list — skipping.")
            continue
        person = dict(person)  # don't mutate original
        person["_llm_reason"] = pick.get("reason", "")
        ranked.append(person)

    print(f"\n[llm] Mistral picked {len(ranked)} candidate(s):")
    for i, p in enumerate(ranked, start=1):
        flat = flatten(p)
        print(f"  {i}. {flat.get('first_name','')} {flat.get('last_name_obfuscated','')} "
              f"— {flat.get('title','')} — {p.get('_llm_reason','')}")
    print()

    return ranked


# ----------------------------------------------------------------------------
# STEP 2: PAID ENRICHMENT (only run for people you explicitly ask for)
# ----------------------------------------------------------------------------

def enrich_person(person_id: str, reveal_personal_emails: bool = False) -> Dict[str, Any]:
    """
    Calls Apollo's People Enrichment endpoint. Costs 1 credit if a match
    with real data is returned. Reveals work email (and personal email /
    phone if you flip the flags, which may cost more depending on plan).
    """
    url = f"{BASE_URL}/people/match"
    params = {
        "id": person_id,
        "reveal_personal_emails": str(reveal_personal_emails).lower(),
        "reveal_phone_number": "false",
    }
    resp = requests.post(url, headers=HEADERS, params=params, timeout=30)
    if resp.status_code != 200:
        print(f"[warn] enrich failed for {person_id}: {resp.status_code} {resp.text[:200]}")
        return {}
    data = resp.json()
    return data.get("person") or {}


# ----------------------------------------------------------------------------
# FLATTENING / CSV OUTPUT
# Apollo's JSON is deeply nested (organization{}, employment_history[], etc).
# This flattens everything so you see EVERY column Apollo actually provides,
# instead of a hardcoded subset.
# ----------------------------------------------------------------------------

def flatten(obj: Any, parent_key: str = "", sep: str = ".") -> Dict[str, Any]:
    items: Dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            items.update(flatten(v, new_key, sep))
    elif isinstance(obj, list):
        if len(obj) == 0:
            items[parent_key] = ""
        elif all(not isinstance(x, (dict, list)) for x in obj):
            # list of plain values -> join into one cell
            items[parent_key] = "; ".join(str(x) for x in obj)
        else:
            for i, x in enumerate(obj):
                items.update(flatten(x, f"{parent_key}[{i}]", sep))
    else:
        items[parent_key] = obj
    return items


def write_csv(rows: List[Dict[str, Any]], path: str) -> List[str]:
    """Flattens all rows, unions all columns across them, writes CSV. Returns column list."""
    flat_rows = [flatten(r) for r in rows]
    all_cols: List[str] = []
    seen = set()
    for fr in flat_rows:
        for col in fr.keys():
            if col not in seen:
                seen.add(col)
                all_cols.append(col)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_cols)
        writer.writeheader()
        for fr in flat_rows:
            writer.writerow(fr)

    return all_cols


def print_table_preview(rows: List[Dict[str, Any]], max_rows: int = 15) -> None:
    """
    Quick terminal preview. Uses different columns depending on whether the
    rows are raw SEARCH results (obfuscated preview, e.g. last_name_obfuscated,
    has_email booleans) or ENRICHED results (real name/email/city).
    """
    if not rows:
        return
    sample = flatten(rows[0])
    is_enriched = "email" in sample or "name" in sample

    if is_enriched:
        cols = ["name", "title", "organization_name", "city", "state", "country", "linkedin_url", "email"]
    else:
        cols = ["first_name", "last_name_obfuscated", "title", "organization.name",
                "has_email", "has_city", "has_state", "has_direct_phone"]

    print("\n" + "-" * 120)
    header = " | ".join(c.split(".")[-1].upper()[:16].ljust(16) for c in cols)
    print(header)
    print("-" * 120)
    for r in rows[:max_rows]:
        flat = flatten(r)
        line = " | ".join(str(flat.get(c, ""))[:16].ljust(16) for c in cols)
        print(line)
    if len(rows) > max_rows:
        print(f"... and {len(rows) - max_rows} more (see CSV for full list)")
    print("-" * 120 + "\n")
    if not is_enriched:
        print(
            "[note] These are SEARCH results (free, 0 credits). Names/emails are "
            "hidden until enriched. Re-run with --enrich-top N (e.g. --enrich-top 5) "
            "to spend credits revealing real name/email for your best matches.\n"
        )


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# CONFIG — edit these to target a different brand/creator campaign.
# API keys are NOT hardcoded here — they're pulled from environment variables
# (APOLLO_API_KEY / MISTRAL_API_KEY) set near the top of this file.
# ----------------------------------------------------------------------------

BRAND_NAME = "BetterHelp"
DOMAIN = "betterhelp.com"
LOCATION = "United States"
TOP_N = 5             # how many people Mistral should pick, and how many get enriched
OUT_CSV = "betterhelp_search_results.csv"


# ----------------------------------------------------------------------------
# MAIN — hardcoded pipeline:
#   1) Free Apollo search across the whole marketing department (0 credits)
#   2) Mistral ranks candidates by sponsorship-outreach fit
#   3) Only Mistral's top N get enriched via Apollo (costs up to N credits)
# ----------------------------------------------------------------------------

def main():
    if API_KEY == "PUT_YOUR_APOLLO_API_KEY_HERE":
        sys.exit("Set APOLLO_API_KEY env var (see CONFIG at top of file).")
    if not MISTRAL_API_KEY:
        sys.exit("Set MISTRAL_API_KEY env var (see CONFIG at top of file).")

    print(f"[info] Searching Apollo (FREE) for the marketing department at "
          f"{BRAND_NAME} ({DOMAIN}) ...")

    people = search_people(
        domain=DOMAIN,
        location=LOCATION,
        titles=MARKETING_DEPARTMENT_TITLES,   # broad marketing-dept sweep
        include_similar_titles=True,
        per_page=25,
        max_pages=2,
    )

    if not people:
        print("[info] No matches found. Try broadening MARKETING_DEPARTMENT_TITLES "
              "or removing LOCATION in the CONFIG section.")
        return

    print(f"[info] Found {len(people)} people (free search, 0 credits used).")

    cols = write_csv(people, OUT_CSV)
    print(f"[info] Wrote {len(people)} rows x {len(cols)} columns to {OUT_CSV}")
    print_table_preview(people)

    print(f"[llm] Asking Mistral ({MISTRAL_MODEL}) to pick the top {TOP_N} "
          f"sponsorship contacts ...")
    picks = llm_rank_candidates(people, brand=BRAND_NAME, top_n=TOP_N)

    if not picks:
        print("[warn] Mistral returned no usable picks — nothing to enrich.")
        return

    n = len(picks)
    print(f"[info] Enriching Mistral's {n} pick(s) "
          f"(THIS WILL COST UP TO {n} Apollo credits) ...")
    enriched_rows = []
    for i, person in enumerate(picks, start=1):
        pid = person.get("id")
        name = person.get("name") or flatten(person).get("first_name", "")
        print(f"[enrich] ({i}/{n}) {name} ...")
        enriched = enrich_person(pid)
        if enriched:
            enriched["_llm_reason"] = person.get("_llm_reason", "")
        enriched_rows.append(enriched if enriched else person)
        time.sleep(0.3)

    enrich_out = OUT_CSV.replace(".csv", "_top5_enriched.csv")
    cols = write_csv(enriched_rows, enrich_out)
    print(f"[info] Wrote enriched data ({len(cols)} columns) to {enrich_out}")
    print_table_preview(enriched_rows)
    print(f"\n[done] Your top {n} BetterHelp sponsorship contacts are in {enrich_out}")


if __name__ == "__main__":
    main()