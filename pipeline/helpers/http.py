import logging
import random
import time
from dataclasses import dataclass

import httpx
from fake_useragent import UserAgent
from tenacity import retry, stop_after_attempt, wait_exponential

_ua = UserAgent()
_logger = logging.getLogger(__name__)


def get_headers(url: str | None = None) -> dict:
    try:
        ua = _ua.random
    except Exception:
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/116.0.0.0 Safari/537.36"
        )
        _logger.debug("fake_useragent failed, falling back to static UA")

    # For Wikimedia API calls, use a descriptive User-Agent per their API
    # guidelines so automated requests are clearly identified.
    if url and ("wikipedia.org" in url or "wikimedia.org" in url):
        ua = "MAVLIT-enrichment/1.0 (https://github.com/axioware/MAVLIT-enrichment; contact: dev@local)"

    return {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.google.com/",
        "Connection": "keep-alive",
    }


@retry(
    wait=wait_exponential(min=2, max=30),
    stop=stop_after_attempt(3),
    reraise=True,
)
def fetch(url: str) -> str:
    """
    Fetch a URL with jittered sleep before every call and exponential
    backoff on failure. Never fires concurrent requests to the same domain.
    """
    time.sleep(random.uniform(1.5, 4.0))
    response = httpx.get(url, headers=get_headers(url), timeout=10, follow_redirects=True)
    response.raise_for_status()
    return response.text


# Jina AI Reader (https://r.jina.ai) — fallback fetcher for pages a direct GET
# can't open. Jina loads the page in a headless browser on its own servers
# (JS rendered, different IP, so it gets past most bot blocks) and returns
# the rendered page HTML. Works without JINA_API_KEY on the free tier
# (~20 req/min); set it in .env for higher limits.
_JINA_READER_URL = "https://r.jina.ai/"
_JINA_TIMEOUT    = 30

# Direct-fetch failures that mean the page genuinely doesn't exist — Jina
# would just get the same answer, so these never fall back.
_NO_FALLBACK_STATUSES = frozenset({404, 410})


@dataclass
class FetchedPage:
    html: str
    url: str        # final URL after redirects (best effort on the Jina path)
    via_jina: bool


def fetch_page_via_jina(url: str) -> FetchedPage | None:
    """
    Fetch url's rendered HTML through Jina Reader. Returns None if Jina
    fails or reports the target page itself returned an error status.
    """
    from config import JINA_API_KEY

    headers = {
        "Accept":          "application/json",
        "X-Return-Format": "html",
        "X-Timeout":       str(_JINA_TIMEOUT),
    }
    if JINA_API_KEY:
        headers["Authorization"] = f"Bearer {JINA_API_KEY}"
    try:
        resp = httpx.get(_JINA_READER_URL + url, headers=headers, timeout=_JINA_TIMEOUT + 15)
        resp.raise_for_status()
        data = resp.json().get("data") or {}
    except Exception as exc:
        _logger.debug("Jina Reader fetch failed for %s: %s", url, exc)
        return None

    target_status = data.get("httpStatus")
    if isinstance(target_status, int) and target_status >= 400:
        _logger.debug("Jina Reader: %s returned HTTP %d", url, target_status)
        return None
    html = data.get("html")
    if not html:
        return None
    return FetchedPage(html=html, url=data.get("url") or url, via_jina=True)


def fetch_via_jina(url: str) -> str | None:
    """fetch_page_via_jina, HTML only."""
    page = fetch_page_via_jina(url)
    return page.html if page else None


def fetch_page_with_jina_fallback(url: str, headers: dict, timeout: float) -> FetchedPage | None:
    """
    Direct GET first; if that's blocked or errors (403/429/5xx, timeout,
    SSL, connection reset...), retry the same URL through Jina Reader.
    404/410 return None straight away without a Jina call. Never raises.
    """
    try:
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        return FetchedPage(html=resp.text, url=str(resp.url), via_jina=False)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in _NO_FALLBACK_STATUSES:
            _logger.debug("Page fetch failed for %s: %s", url, exc)
            return None
        reason = f"HTTP {exc.response.status_code}"
    except Exception as exc:
        reason = type(exc).__name__

    _logger.info("Direct fetch failed for %s (%s) — retrying via Jina Reader", url, reason)
    page = fetch_page_via_jina(url)
    if page:
        _logger.info("Jina Reader fetched %s (%d chars)", url, len(page.html))
    return page


def fetch_html_with_jina_fallback(url: str, headers: dict, timeout: float) -> str | None:
    """fetch_page_with_jina_fallback, HTML only."""
    page = fetch_page_with_jina_fallback(url, headers, timeout)
    return page.html if page else None
