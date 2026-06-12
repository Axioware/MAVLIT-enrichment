"""
test_ad_library.py

Searches Meta Ad Library for running/historical ads by a brand.
Mirrors the Facebook Ad Library website filters.

Usage:
    python3 test_ad_library.py

Edit the CONFIG block below to change search term, country, and dates.
"""

import sys

import httpx
from dotenv import load_dotenv

load_dotenv()
from config import META_ACCESS_TOKEN

GRAPH_URL = "https://graph.facebook.com/v21.0"

# ── CONFIG — edit these ───────────────────────────────────────────────────────
SEARCH_TERM  = "iradahclothing"
COUNTRY      = "ALL"           # "ALL" or country code e.g. "US", "GB", "PK"
AD_CATEGORY  = "ALL"           # ALL, EMPLOYMENT_ADS, HOUSING_ADS, FINANCIAL_PRODUCTS_AND_SERVICES_ADS
DATE_FROM    = ""              # YYYY-MM-DD  (leave "" for no filter)
DATE_TO      = ""              # YYYY-MM-DD  (leave "" for no filter)
LIMIT        = 10
# ─────────────────────────────────────────────────────────────────────────────

FIELDS = ",".join([
    "id",
    "page_name",
    "page_id",
    "ad_creative_bodies",
    "ad_creative_link_captions",
    "ad_creative_link_titles",
    "publisher_platforms",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "impressions",
    "spend",
    "currency",
    "ad_snapshot_url",
    "bylines",
])

COUNTRIES_MAP = {
    "ALL": '["US","GB","CA","AU","PK","IN","DE","FR","AE","SA"]',
    "US":  '["US"]',
    "GB":  '["GB"]',
    "PK":  '["PK"]',
    "IN":  '["IN"]',
    "AE":  '["AE"]',
    "CA":  '["CA"]',
    "AU":  '["AU"]',
}


def fetch_ads(search: str, country: str, category: str,
              date_from: str, date_to: str) -> list[dict]:
    countries = COUNTRIES_MAP.get(country.upper(), COUNTRIES_MAP["ALL"])
    params: dict = {
        "search_terms":         search,
        "ad_type":              category,
        "ad_reached_countries": countries,
        "limit":                LIMIT,
        "fields":               FIELDS,
        "access_token":         META_ACCESS_TOKEN,
    }
    if date_from:
        params["ad_delivery_date_min"] = date_from
    if date_to:
        params["ad_delivery_date_max"] = date_to

    try:
        resp = httpx.get(f"{GRAPH_URL}/ads_archive", params=params, timeout=20)
        if resp.status_code >= 400:
            err = resp.json().get("error", {}).get("message", resp.text[:300])
            print(f"\n  API ERROR: {err}")
            return []
        return resp.json().get("data", [])
    except Exception as e:
        print(f"\n  REQUEST FAILED: {e}")
        return []


def is_active(ad: dict) -> str:
    return "Active" if not ad.get("ad_delivery_stop_time") else "Inactive"


def print_ads(ads: list[dict]) -> None:
    divider = "─" * 80

    if not ads:
        print(f"\n{divider}")
        print("  No ads found.")
        print(divider)
        return

    for i, ad in enumerate(ads, 1):
        bodies   = ad.get("ad_creative_bodies") or []
        titles   = ad.get("ad_creative_link_titles") or []
        captions = ad.get("ad_creative_link_captions") or []
        caption  = bodies[0][:200] if bodies else (titles[0][:200] if titles else "(no caption)")
        platforms = ", ".join(ad.get("publisher_platforms") or [])
        status   = is_active(ad)
        start    = (ad.get("ad_delivery_start_time") or "")[:10]
        end      = (ad.get("ad_delivery_stop_time") or "ongoing")[:10]
        imp      = ad.get("impressions") or {}
        spend    = ad.get("spend") or {}
        currency = ad.get("currency") or ""
        bylines  = ", ".join(ad.get("bylines") or [])

        imp_str   = f"{imp.get('lower_bound','?')}–{imp.get('upper_bound','?')}" if imp else "N/A"
        spend_str = f"{spend.get('lower_bound','?')}–{spend.get('upper_bound','?')} {currency}".strip() if spend else "N/A"

        print(f"\n{divider}")
        print(f"  [{i}] {status}  |  Library ID: {ad.get('id')}")
        print(f"  Started: {start}  →  {end}")
        print(f"  Page   : {ad.get('page_name')}  (id: {ad.get('page_id')})")
        print(f"  Platforms : {platforms}")
        if bylines:
            print(f"  Byline    : {bylines}")
        print(f"  Impressions: {imp_str}  |  Spend: {spend_str}")
        print(f"  Caption   : {caption!r}")
        print(f"  Ad URL    : {ad.get('ad_snapshot_url') or 'N/A'}")

    print(f"\n{divider}")
    print(f"  Total: {len(ads)} ad(s) returned (limit={LIMIT})")
    print(divider)


# ── Main ──────────────────────────────────────────────────────────────────────

if not META_ACCESS_TOKEN:
    print("ERROR: META_ACCESS_TOKEN not set in .env")
    sys.exit(1)

print("=" * 80)
print("  Meta Ad Library Search")
print(f"  Search : {SEARCH_TERM}")
print(f"  Country: {COUNTRY}  |  Category: {AD_CATEGORY}")
print(f"  Dates  : {DATE_FROM or 'any'} → {DATE_TO or 'today'}")
print("=" * 80)

ads = fetch_ads(SEARCH_TERM, COUNTRY, AD_CATEGORY, DATE_FROM, DATE_TO)
print_ads(ads)
