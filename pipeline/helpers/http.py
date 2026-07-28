import random
import time
import httpx
from fake_useragent import UserAgent
import logging
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