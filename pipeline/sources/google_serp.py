import asyncio
import random

from fake_useragent import UserAgent
from playwright.async_api import async_playwright

_ua = UserAgent()


async def _google_brand_search_async(query: str) -> list[str]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_extra_http_headers({"User-Agent": _ua.random})
        await page.goto(f"https://www.google.com/search?q={query}")
        # Let the page settle — Google renders results via JS
        await page.wait_for_timeout(random.randint(2000, 4000))
        result_elements = await page.query_selector_all("h3")
        titles = [await el.inner_text() for el in result_elements]
        await browser.close()
    return [t.strip() for t in titles if t.strip()]


def google_brand_search(query: str) -> list[str]:
    """
    Synchronous wrapper around the async Playwright scraper.

    Note: Google may block repeated headless requests. A 3-6s jitter is
    applied per search. Consider SerpAPI (~$50/mo) if blocking is an issue.
    """
    return asyncio.run(_google_brand_search_async(query))