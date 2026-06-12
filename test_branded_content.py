"""
╔══════════════════════════════════════════════════════════════════╗
║           META BRANDED CONTENT SCRAPER  (branded_content.py)    ║
║  Scrapes Instagram AND Facebook branded content (paid            ║
║  partnerships) from Meta's public transparency page.            ║
║  No API key or login required.                                  ║
╚══════════════════════════════════════════════════════════════════╝

URL format Meta uses per platform:
  Instagram:
    https://www.facebook.com/ads/library/branded_content/
        ?id=17841400602400210&query=Nike&target=instagram
        &start_date=YYYY-MM-DD&end_date=YYYY-MM-DD

  Facebook:
    https://www.facebook.com/ads/library/branded_content/
        ?id=15087023444&query=Nike&target=facebook
        &start_date=YYYY-MM-DD&end_date=YYYY-MM-DD

Requirements:
    pip install playwright
    playwright install chromium

How to find PAGE IDs for any brand:
    1. Go to https://www.facebook.com/ads/library/branded_content/
    2. Select Platform = Instagram → search your brand → click autocomplete
    3. Copy the  id=XXXXX  value from the URL  → use as INSTAGRAM_PAGE_ID
    4. Repeat with Platform = Facebook         → use as FACEBOOK_PAGE_ID
"""

import asyncio
from datetime import datetime, timedelta
from urllib.parse import urlencode
from playwright.async_api import async_playwright, TimeoutError as PWTimeout


# ════════════════════════════════════════════════════════════════
#  ▼▼▼  EDIT THESE  ▼▼▼
# ════════════════════════════════════════════════════════════════

BRAND_NAME         = "Nike"

INSTAGRAM_PAGE_ID  = "17841400602400210"   # Brand's Instagram page ID
FACEBOOK_PAGE_ID   = "15087023444"         # Brand's Facebook page ID

# Date range  (defaults: last 7 days)
END_DATE    = datetime.today().strftime("%Y-%m-%d")
START_DATE  = (datetime.today() - timedelta(days=7)).strftime("%Y-%m-%d")

HEADLESS    = True      # True = invisible browser | False = watch the browser
TIMEOUT_MS  = 15000     # Max wait per step in milliseconds

# ════════════════════════════════════════════════════════════════


BASE_URL = "https://www.facebook.com/ads/library/branded_content/"

# Platform config — maps name → page ID and target param
PLATFORMS = {
    "Instagram": {
        "page_id": INSTAGRAM_PAGE_ID,
        "target" : "instagram",
        "icon"   : "📸",
    },
    "Facebook": {
        "page_id": FACEBOOK_PAGE_ID,
        "target" : "facebook",
        "icon"   : "👤",
    },
}


# ─────────────────────────────────────────────────────────────────
#  URL BUILDER
# ─────────────────────────────────────────────────────────────────

def build_url(page_id: str, target: str) -> str:
    params = {
        "id"        : page_id,
        "query"     : BRAND_NAME,
        "target"    : target,
        "start_date": START_DATE,
        "end_date"  : END_DATE,
    }
    return BASE_URL + "?" + urlencode(params)


# ─────────────────────────────────────────────────────────────────
#  DISPLAY HELPERS
# ─────────────────────────────────────────────────────────────────

def print_header():
    print("\n" + "═" * 72)
    print(f"  🤝  META BRANDED CONTENT SCRAPER")
    print(f"  🔍  Brand        : {BRAND_NAME}")
    print(f"  📸  Instagram ID : {INSTAGRAM_PAGE_ID}")
    print(f"  👤  Facebook ID  : {FACEBOOK_PAGE_ID}")
    print(f"  📅  Date Range   : {START_DATE}  →  {END_DATE}")
    print(f"  🕐  Fetched      : {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}")
    print("═" * 72)


def print_platform_results(platform_name: str, icon: str, url: str, results: list[dict]):
    print("\n" + "─" * 72)
    print(f"  {icon}  {platform_name.upper()} RESULTS")
    print(f"  🔗  {url}")
    print("─" * 72)

    if not results:
        print(f"\n  ⚠️   No branded content found for {platform_name} in this date range.\n")
        return

    print(f"\n  ✅  Total Results: {len(results)}\n")

    W = {"date": 13, "creator": 28, "brand": 22, "type": 10}

    # Header row
    print(
        f"  {'DATE':<{W['date']}} "
        f"{'CREATOR':<{W['creator']}} "
        f"{'BRAND PARTNER':<{W['brand']}} "
        f"{'TYPE':<{W['type']}} "
        f"LINK"
    )
    print(
        f"  {'─'*W['date']} "
        f"{'─'*W['creator']} "
        f"{'─'*W['brand']} "
        f"{'─'*W['type']} "
        f"{'─'*45}"
    )

    for r in results:
        date    = str(r.get("date", "N/A"))[:W["date"]]
        creator = str(r.get("creator", "N/A"))[:W["creator"]]
        brand   = str(r.get("brand_partner", "N/A"))[:W["brand"]]
        ctype   = str(r.get("type", "N/A"))[:W["type"]]
        link    = str(r.get("link", "N/A"))

        print(
            f"  {date:<{W['date']}} "
            f"{creator:<{W['creator']}} "
            f"{brand:<{W['brand']}} "
            f"{ctype:<{W['type']}} "
            f"{link}"
        )
    print()


# ─────────────────────────────────────────────────────────────────
#  SCRAPER (single platform)
# ─────────────────────────────────────────────────────────────────

async def scrape_platform(page, platform_name: str, page_id: str, target: str) -> list[dict]:
    """Scrape branded content for one platform using an already-open browser page."""
    results   = []
    url       = build_url(page_id, target)

    # ── 1. Navigate directly to the result URL ─────────────────────
    print(f"\n  🌐  [{platform_name}] Opening URL...")
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:
        print(f"  ❌  [{platform_name}] Failed to load page: {exc}")
        return results

    await page.wait_for_timeout(2500)

    # ── 2. Dismiss cookie / GDPR banner (only matters on first load) ─
    for btn_text in ["Allow all cookies", "Accept all", "Allow essential and optional cookies"]:
        try:
            btn = page.get_by_role("button", name=btn_text)
            if await btn.count():
                await btn.first.click()
                print(f"  🍪  [{platform_name}] Cookie banner dismissed.")
                await page.wait_for_timeout(1000)
                break
        except Exception:
            pass

    # ── 3. Wait for results table to appear ────────────────────────
    print(f"  ⏳  [{platform_name}] Waiting for results...")
    try:
        await page.wait_for_selector(
            "div[role='table'], div[role='grid'], table, div[role='row']",
            timeout=TIMEOUT_MS,
        )
        await page.wait_for_timeout(1500)
    except PWTimeout:
        print(f"  ⚠️   [{platform_name}] Results table not found within timeout.")
        return results

    # ── 4. Parse the results table ──────────────────────────────────
    print(f"  📊  [{platform_name}] Parsing rows...")

    HEADER_KEYWORDS = {"date", "creator", "brand", "partner", "type", "link", "content"}
    rows = await page.query_selector_all("div[role='row'], tr")

    for row in rows:
        cells = await row.query_selector_all(
            "div[role='cell'], div[role='gridcell'], td"
        )
        if len(cells) < 4:
            continue

        cell_texts = []
        for cell in cells:
            txt = (await cell.inner_text()).strip().replace("\n", " ")
            cell_texts.append(txt)

        # Skip header rows
        if any(kw in cell_texts[0].lower() for kw in HEADER_KEYWORDS):
            continue

        # Skip empty rows
        if not any(t for t in cell_texts if t):
            continue

        # Extract link from last cell
        link = "N/A"
        link_el = await cells[-1].query_selector("a")
        if link_el:
            href = await link_el.get_attribute("href") or ""
            if href.startswith("http"):
                link = href
            elif href.startswith("/"):
                link = "https://www.facebook.com" + href
            else:
                link_text = (await link_el.inner_text()).strip()
                link = link_text if link_text else "N/A"

        results.append({
            "date"          : cell_texts[0] if len(cell_texts) > 0 else "N/A",
            "creator"       : cell_texts[1] if len(cell_texts) > 1 else "N/A",
            "brand_partner" : cell_texts[2] if len(cell_texts) > 2 else "N/A",
            "type"          : cell_texts[3] if len(cell_texts) > 3 else "N/A",
            "link"          : link,
        })

    return results


# ─────────────────────────────────────────────────────────────────
#  MAIN — runs both platforms in one browser session
# ─────────────────────────────────────────────────────────────────

async def run_all():
    all_results = {}   # { platform_name: [results] }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()

        # Scrape Instagram then Facebook using the same browser tab
        for platform_name, cfg in PLATFORMS.items():
            results = await scrape_platform(
                page,
                platform_name,
                cfg["page_id"],
                cfg["target"],
            )
            all_results[platform_name] = results

        await browser.close()

    return all_results


if __name__ == "__main__":
    print_header()
    print(f"\n  ⏳  Launching Chromium (headless={HEADLESS})...")

    all_results = asyncio.run(run_all())

    # ── Print Instagram results ────────────────────────────────────
    ig_cfg = PLATFORMS["Instagram"]
    print_platform_results(
        "Instagram",
        ig_cfg["icon"],
        build_url(ig_cfg["page_id"], ig_cfg["target"]),
        all_results.get("Instagram", []),
    )

    # ── Print Facebook results ─────────────────────────────────────
    fb_cfg = PLATFORMS["Facebook"]
    print_platform_results(
        "Facebook",
        fb_cfg["icon"],
        build_url(fb_cfg["page_id"], fb_cfg["target"]),
        all_results.get("Facebook", []),
    )

    # ── Summary ────────────────────────────────────────────────────
    ig_count = len(all_results.get("Instagram", []))
    fb_count = len(all_results.get("Facebook", []))
    print("═" * 72)
    print(f"  📊  SUMMARY  |  Instagram: {ig_count} result(s)   Facebook: {fb_count} result(s)")
    print("═" * 72 + "\n")