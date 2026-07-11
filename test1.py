"""
╔══╗
║           META BRANDED CONTENT SCRAPER  (branded_content.py)    ║
║  Get Instagram AND Facebook paid partnership posts for a brand   ║
║  just by typing its name — no ID lookup or setup needed.        ║
║  No API key or login required.                                  ║
╚══╝

HOW TO USE:
    Just set BRAND_NAME below to the brand's name or @username,
    then run the script. That's it — everything else (finding the
    right Meta page ID for Instagram/Facebook, etc.) happens
    automatically behind the scenes.

Requirements:
    pip install playwright
    playwright install chromium
"""

import asyncio
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, parse_qs
from playwright.async_api import async_playwright, TimeoutError as PWTimeout


# 
#    EDIT THIS  
# 

BRAND_NAME  = "Nike"     # brand name or @username — that's all you need

# Date range  (defaults: last 7 days)
END_DATE    = datetime.today().strftime("%Y-%m-%d")
START_DATE  = (datetime.today() - timedelta(days=7)).strftime("%Y-%m-%d")

HEADLESS    = True      # True = invisible browser | False = watch the browser (for debugging)
TIMEOUT_MS  = 15000

# 


BASE_URL = "https://www.facebook.com/ads/library/branded_content/"

PLATFORMS = {
    "Instagram": {"target": "instagram", "icon": "📸"},
    "Facebook":  {"target": "facebook",  "icon": "👤"},
}


# 
#  DISPLAY HELPERS
# 

def print_header():
    print("\n" + "═" * 72)
    print(f"  🤝  META BRANDED CONTENT SCRAPER")
    print(f"  🔍  Brand        : {BRAND_NAME}")
    print(f"  📅  Date Range   : {START_DATE}  →  {END_DATE}")
    print(f"  🕐  Fetched      : {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}")
    print("═" * 72)


def print_platform_results(platform_name: str, icon: str, results: list[dict]):
    print("\n" + "─" * 72)
    print(f"  {icon}  {platform_name.upper()} — PAID PARTNERSHIP POSTS")
    print("─" * 72)

    if not results:
        print(f"\n  ⚠️   No branded content found for {platform_name} in this date range.\n")
        return

    print(f"\n  ✅  Total Posts: {len(results)}\n")

    W = {"date": 13, "creator": 28, "type": 10}

    print(f"  {'DATE':<{W['date']}} {'CREATOR':<{W['creator']}} {'TYPE':<{W['type']}} LINK")
    print(f"  {'─'*W['date']} {'─'*W['creator']} {'─'*W['type']} {'─'*45}")

    for r in results:
        date    = str(r.get("date", "N/A"))[:W["date"]]
        creator = str(r.get("creator", "N/A"))[:W["creator"]]
        ctype   = str(r.get("type", "N/A"))[:W["type"]]
        link    = str(r.get("link", "N/A"))
        print(f"  {date:<{W['date']}} {creator:<{W['creator']}} {ctype:<{W['type']}} {link}")
    print()


# 
#  INTERNAL: resolve a brand's page ID for a platform
#  (you never need to call this yourself — get_brand_posts() does it for you)
# 

async def _resolve_page_id(page, brand_name: str, target: str) -> str | None:
    search_url = f"{BASE_URL}?target={target}"
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return None

    await page.wait_for_timeout(2000)

    for btn_text in ["Allow all cookies", "Accept all", "Allow essential and optional cookies"]:
        try:
            btn = page.get_by_role("button", name=btn_text)
            if await btn.count():
                await btn.first.click()
                await page.wait_for_timeout(800)
                break
        except Exception:
            pass

    search_box = None
    for locator_fn in [
        lambda: page.get_by_placeholder("Search by brand or advertiser name"),
        lambda: page.get_by_placeholder("Search"),
        lambda: page.get_by_role("combobox").first,
    ]:
        candidate = locator_fn()
        try:
            if await candidate.count():
                search_box = candidate
                break
        except Exception:
            continue

    if search_box is None:
        return None

    await search_box.click()
    await search_box.fill(brand_name)
    await page.wait_for_timeout(1800)

    try:
        options = page.get_by_role("option")
        await options.first.wait_for(timeout=6000)
        count = await options.count()

        candidates = []
        for i in range(count):
            text = (await options.nth(i).inner_text()).strip()
            candidates.append((i, text))

        if not candidates:
            return None

        target_name = brand_name.strip().lower()
        best_index = None

        for i, text in candidates:
            if text.strip().lower() == target_name:
                best_index = i
                break

        if best_index is None:
            for i, text in candidates:
                first_line = text.splitlines()[0].strip().lower() if text else ""
                if first_line == target_name:
                    best_index = i
                    break

        if best_index is None:
            for i, text in candidates:
                first_line = text.splitlines()[0].strip().lower() if text else ""
                if first_line.split(" ")[0] == target_name:
                    best_index = i
                    break

        if best_index is None:
            best_index = 0
            print(
                f"  ⚠️   [{target}] No exact match for '{brand_name}'. "
                f"Using closest suggestion: '{candidates[0][1]}'. Verify if it looks off."
            )

        await options.nth(best_index).click()
    except PWTimeout:
        return None

    await page.wait_for_timeout(2000)

    query = parse_qs(urlparse(page.url).query)
    return query.get("id", [None])[0]


# 
#  INTERNAL: scrape posts once we have a resolved page ID
# 

async def _scrape_posts(page, page_id: str, target: str) -> list[dict]:
    params = {
        "id": page_id, "query": BRAND_NAME, "target": target,
        "start_date": START_DATE, "end_date": END_DATE,
    }
    url = BASE_URL + "?" + urlencode(params)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return []

    await page.wait_for_timeout(2500)

    for btn_text in ["Allow all cookies", "Accept all", "Allow essential and optional cookies"]:
        try:
            btn = page.get_by_role("button", name=btn_text)
            if await btn.count():
                await btn.first.click()
                await page.wait_for_timeout(1000)
                break
        except Exception:
            pass

    try:
        await page.wait_for_selector(
            "div[role='table'], div[role='grid'], table, div[role='row']",
            timeout=TIMEOUT_MS,
        )
        await page.wait_for_timeout(1500)
    except PWTimeout:
        return []

    HEADER_KEYWORDS = {"date", "creator", "brand", "partner", "type", "link", "content"}
    rows = await page.query_selector_all("div[role='row'], tr")
    results = []

    for row in rows:
        cells = await row.query_selector_all("div[role='cell'], div[role='gridcell'], td")
        if len(cells) < 4:
            continue

        cell_texts = []
        for cell in cells:
            txt = (await cell.inner_text()).strip().replace("\n", " ")
            cell_texts.append(txt)

        if any(kw in cell_texts[0].lower() for kw in HEADER_KEYWORDS):
            continue
        if not any(t for t in cell_texts if t):
            continue

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


# 
#  PUBLIC ENTRY POINT — this is the only function you need
# 

async def get_brand_posts(brand_name: str) -> dict:
    """
    Give it a brand name or @username, get back its Instagram and
    Facebook paid partnership posts. ID resolution happens internally —
    you never have to deal with it.

    Returns: { "Instagram": [ {date, creator, type, link, ...}, ... ],
               "Facebook":  [ ... ] }
    """
    all_results = {}

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

        for platform_name, cfg in PLATFORMS.items():
            page_id = await _resolve_page_id(page, brand_name, cfg["target"])
            await page.wait_for_timeout(1000)

            if not page_id:
                print(f"  ⏭️   Skipping {platform_name} — couldn't find a matching page.")
                all_results[platform_name] = []
                continue

            all_results[platform_name] = await _scrape_posts(page, page_id, cfg["target"])

        await browser.close()

    return all_results


if __name__ == "__main__":
    print_header()
    print(f"\n  ⏳  Launching Chromium (headless={HEADLESS})...")

    all_results = asyncio.run(get_brand_posts(BRAND_NAME))

    for platform_name, cfg in PLATFORMS.items():
        print_platform_results(platform_name, cfg["icon"], all_results.get(platform_name, []))

    ig_count = len(all_results.get("Instagram", []))
    fb_count = len(all_results.get("Facebook", []))
    print("═" * 72)
    print(f"  📊  SUMMARY  |  Instagram: {ig_count} post(s)   Facebook: {fb_count} post(s)")
    print("═" * 72 + "\n")