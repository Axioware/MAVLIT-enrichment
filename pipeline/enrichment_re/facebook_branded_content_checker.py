"""
pipeline/enrichment_re/facebook_branded_content_checker.py

Check whether an Instagram account appears in Meta's Branded Content Library
(Facebook Ads Library -> Branded content) within a date window, i.e. whether
it has published branded-content / paid-partnership posts.

Flow:
  1. Open https://www.facebook.com/ads/library/branded_content/ with
     target=instagram and the date window.
  2. Type the username into the "Search for a business or creator" box.
  3. Pick the dropdown option for that exact @username and read its numeric
     Instagram account ID (parent <li id="..."> first, aria-describedby as
     fallback).
  4. Open the results page for that ID and count the branded-content results.

Selectors deliberately avoid Facebook's generated ids / CSS class names
(they change constantly): only placeholder text, ARIA roles, attributes
with a numeric-ID shape and visible text are used.

Run from the project root:
    python -m pipeline.enrichment_re.facebook_branded_content_checker
    python -m pipeline.enrichment_re.facebook_branded_content_checker --username jutdwae
    python -m pipeline.enrichment_re.facebook_branded_content_checker --username jutdwae --headed

Exit codes: 0 = check completed (FOUND or NOT FOUND), 1 = the check itself
failed (timeout, layout change, browser error).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from config import FACEBOOK_PROXIES
from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TARGET_USERNAME = "keva.creates"   # overridable with --username

START_DATE = "2026-01-01"
END_DATE = "2026-10-09"
TARGET = "instagram"

BASE_URL = "https://www.facebook.com/ads/library/branded_content/"
SEARCH_PLACEHOLDER = "Search for a business or creator"

PAGE_LOAD_TIMEOUT_MS = 45_000      # initial navigation
SEARCH_INPUT_TIMEOUT_MS = 30_000   # waiting for the search box to render
DROPDOWN_TIMEOUT_MS = 20_000       # waiting for suggestions after typing
RESULTS_SETTLE_TIMEOUT_MS = 30_000 # waiting for the results page to settle
TYPE_DELAY_MS = 90                 # per-keystroke delay — the typeahead ignores instant fills

# Rate limiting: Facebook temporarily blocks an IP that searches the library
# "too fast". After every CHECKS_PER_BATCH checks in one process, pause for
# BATCH_PAUSE_SECONDS before the next check, and switch to the next proxy in
# FACEBOOK_PROXIES (config / .env) — batch 1 uses proxy 1, batch 2 proxy 2,
# ... Once every proxy has had its batch (one full round), pause for
# ROUND_PAUSE_SECONDS instead, then start again from proxy 1.
CHECKS_PER_BATCH = 35
BATCH_PAUSE_SECONDS = 2 * 60 
ROUND_PAUSE_SECONDS = 30 * 60

# Instagram account IDs are long digit strings (e.g. 17841461241854325).
_NUMERIC_ID = re.compile(r"^\d{6,}$")

# Visible-text signals on the results page. Several independent ones are
# combined so a single wording/layout change doesn't break detection.
_RESULT_COUNT_PATTERNS = (
    re.compile(r"~?\s*([\d][\d,.]*)\s*(K|M)?\s+results?\b", re.IGNORECASE),
    re.compile(r"\babout\s+([\d][\d,.]*)\s*(K|M)?\s+results?\b", re.IGNORECASE),
)
_NO_RESULT_PATTERNS = (
    re.compile(r"\bno results\b", re.IGNORECASE),
    re.compile(r"\b0\s+results?\b", re.IGNORECASE),
    re.compile(r"no (?:branded content|ads|posts) (?:match|found|to show)", re.IGNORECASE),
    re.compile(r"didn['’]t find any", re.IGNORECASE),
)
# Phrases that appear once per branded-content result card. ("Branded
# content" is deliberately not used — it is also the page title/nav label.)
_CARD_TEXT_PATTERNS = (
    re.compile(r"paid partnership", re.IGNORECASE),
    re.compile(r"see (?:post|ad) details", re.IGNORECASE),
)
# Cookie / consent buttons (EU servers get a consent dialog first).
_CONSENT_BUTTON_NAMES = (
    re.compile(r"allow all cookies", re.IGNORECASE),
    re.compile(r"accept all", re.IGNORECASE),
    re.compile(r"decline optional cookies", re.IGNORECASE),
    re.compile(r"only allow essential cookies", re.IGNORECASE),
)

_DEBUG_DIR = Path("logs")

# Checks started in this process since the last pause, and which batch
# (0-based, picks the proxy) this process is on (see _throttle).
_checks_since_pause = 0
_batch_index = 0


def _parse_proxies(value: str) -> list[dict[str, str]]:
    """'ip:port:user:pass,ip:port,...' -> Playwright proxy settings."""
    proxies: list[dict[str, str]] = []
    for entry in (value or "").split(","):
        parts = entry.strip().split(":")
        if len(parts) == 2:
            proxies.append({"server": f"http://{parts[0]}:{parts[1]}"})
        elif len(parts) == 4:
            proxies.append({"server": f"http://{parts[0]}:{parts[1]}", "username": parts[2], "password": parts[3]})
        elif entry.strip():
            logger.warning("Ignoring malformed FACEBOOK_PROXIES entry (expected ip:port[:user:pass])")
    return proxies


_PROXIES = _parse_proxies(FACEBOOK_PROXIES)


def _current_proxy() -> dict[str, str] | None:
    """Proxy for the current batch, or None (direct) when none are configured."""
    return _PROXIES[_batch_index % len(_PROXIES)] if _PROXIES else None


class CheckError(RuntimeError):
    """The check could not be completed (timeout, layout change, ...)."""


@dataclass
class CheckResult:
    username: str
    instagram_id: str | None
    result_count: int

    @property
    def found(self) -> bool:
        return self.instagram_id is not None and self.result_count > 0


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------

def _library_url(**extra: str) -> str:
    """Branded Content Library URL with the target + date window (+ any extra params)."""
    params = {**extra, "target": TARGET, "end_date": END_DATE, "start_date": START_DATE}
    return f"{BASE_URL}?{urlencode(params)}"


async def _dismiss_consent(page: Page) -> None:
    """Click through a cookie-consent dialog if one is shown; no-op otherwise."""
    for name in _CONSENT_BUTTON_NAMES:
        button = page.get_by_role("button", name=name)
        try:
            if await button.count() and await button.first.is_visible():
                logger.info("Dismissing cookie consent dialog")
                await button.first.click(timeout=5_000)
                await page.wait_for_timeout(1_000)
                return
        except PlaywrightTimeoutError:
            continue


async def _find_search_input(page: Page) -> Locator:
    """
    Locate the search box by placeholder, then by ARIA role, then by input
    type — never by generated id/class.
    """
    candidates = (
        page.get_by_placeholder(SEARCH_PLACEHOLDER, exact=False),
        page.get_by_role("searchbox"),
        page.locator('input[type="search"]'),
    )
    for candidate in candidates:
        try:
            await candidate.first.wait_for(state="visible", timeout=SEARCH_INPUT_TIMEOUT_MS // len(candidates))
            return candidate.first
        except PlaywrightTimeoutError:
            continue
    raise CheckError("Search input not found — the page layout may have changed or a login wall is shown")


def _numeric_token(value: str | None) -> str | None:
    """First whitespace-separated token in `value` that looks like an account ID."""
    for token in (value or "").split():
        if _NUMERIC_ID.match(token):
            return token
    return None


async def _extract_instagram_id(page: Page, username: str) -> str | None:
    """
    From the open suggestion dropdown, return the numeric Instagram ID of the
    option showing exactly @username. Prefers the option's <li id>, falls back
    to aria-describedby. None when no option matches.
    """
    handle_text = re.compile(rf"@{re.escape(username)}(?![\w.])", re.IGNORECASE)

    # Wait until at least one suggestion mentioning the handle is rendered.
    matching = page.locator("li[id], [role='option'], [aria-describedby]").filter(has_text=handle_text)
    try:
        await matching.first.wait_for(state="visible", timeout=DROPDOWN_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        options = await page.locator("[role='option'], li[id]").all_inner_texts()
        logger.warning(
            "No dropdown option for @%s within %ds (visible options: %s)",
            username, DROPDOWN_TIMEOUT_MS // 1000, [o.strip()[:60] for o in options[:10]] or "none",
        )
        return None

    # 1) Preferred: a <li> with a numeric id that contains @username.
    for li in await page.locator("li[id]").filter(has_text=handle_text).all():
        if (value := _numeric_token(await li.get_attribute("id"))):
            logger.info("Instagram ID taken from <li id>")
            return value

    # 2) Fallback: an element with aria-describedby (the option itself, or
    #    one inside it) whose value is a numeric ID.
    for option in await page.locator("[role='option'], li").filter(has_text=handle_text).all():
        for element in [option, *await option.locator("[aria-describedby]").all()]:
            if (value := _numeric_token(await element.get_attribute("aria-describedby"))):
                logger.info("Instagram ID taken from aria-describedby")
                return value
    for element in await page.locator("[aria-describedby]").filter(has_text=handle_text).all():
        if (value := _numeric_token(await element.get_attribute("aria-describedby"))):
            logger.info("Instagram ID taken from aria-describedby")
            return value

    logger.warning("Dropdown option for @%s found, but it carries no numeric ID — layout may have changed", username)
    return None


def _parse_count(number: str, suffix: str | None) -> int:
    """'1,234' -> 1234, '1.2' + 'K' -> 1200."""
    value = float(number.replace(",", ""))
    multiplier = {"K": 1_000, "M": 1_000_000}.get((suffix or "").upper(), 1)
    return int(value * multiplier)


async def _count_results(page: Page) -> int:
    """
    Count branded-content results using several independent signals and
    return the most trustworthy one:
      1. an explicit "no results" message -> 0
      2. a "N results" / "~N results" summary line
      3. result cards: elements with role=article, and repeated per-card
         phrases ("Paid partnership", "See post details", ...)
    """
    body_text = await page.locator("body").inner_text()

    # Signal 2: summary line with a total.
    summary_counts = [
        _parse_count(m.group(1), m.group(2))
        for pattern in _RESULT_COUNT_PATTERNS
        for m in pattern.finditer(body_text)
    ]
    summary = max(summary_counts) if summary_counts else None

    # Signal 1: explicit empty-state text (only trusted without a positive total).
    empty_state = any(p.search(body_text) for p in _NO_RESULT_PATTERNS)

    # Signal 3: visible result containers.
    article_cards = await page.get_by_role("article").count()
    phrase_cards = max((len(p.findall(body_text)) for p in _CARD_TEXT_PATTERNS), default=0)
    cards = max(article_cards, phrase_cards)

    logger.info(
        "Result signals: summary=%s empty_state=%s article_cards=%d phrase_cards=%d",
        summary, empty_state, article_cards, phrase_cards,
    )
    if summary is not None and summary > 0:
        return summary
    if empty_state:
        return 0
    return cards


async def _save_debug_screenshot(page: Page | None, username: str) -> None:
    """Best-effort full-page screenshot for diagnosing layout changes."""
    if page is None:
        return
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path = _DEBUG_DIR / f"facebook_branded_content_{username}_error.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.info("Saved debug screenshot to %s", path)
    except Exception:  # noqa: BLE001 — diagnostics must never mask the real error
        logger.debug("Could not save debug screenshot", exc_info=True)


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------

async def _throttle() -> None:
    """
    Count this check; once CHECKS_PER_BATCH checks have run since the last
    pause, sleep first and move to the next proxy — BATCH_PAUSE_SECONDS
    between proxies, ROUND_PAUSE_SECONDS after the last proxy's batch (all
    proxies used once) before starting again from proxy 1. The counter is per
    process, so it spans every check a long run (e.g.
    discover_collaborator_creators.py) makes, including failed ones.
    """
    global _checks_since_pause, _batch_index
    if _checks_since_pause >= CHECKS_PER_BATCH:
        round_done = bool(_PROXIES) and (_batch_index + 1) % len(_PROXIES) == 0
        pause = ROUND_PAUSE_SECONDS if round_done else BATCH_PAUSE_SECONDS
        resume_at = datetime.now() + timedelta(seconds=pause)
        logger.info(
            "Rate limit: %d checks done%s — pausing %d min to avoid a Facebook block (resuming ~%s)",
            _checks_since_pause, f", all {len(_PROXIES)} proxies used" if round_done else "",
            pause // 60, resume_at.strftime("%Y-%m-%d %H:%M"),
        )
        await asyncio.sleep(pause)
        _checks_since_pause = 0
        _batch_index += 1
    _checks_since_pause += 1
    proxy = _current_proxy()
    logger.info(
        "Facebook check %d/%d in batch %d (%s)",
        _checks_since_pause, CHECKS_PER_BATCH, _batch_index + 1,
        f"proxy {_batch_index % len(_PROXIES) + 1}/{len(_PROXIES)} {proxy['server']}" if proxy else "no proxy",
    )


async def check_branded_content(username: str, headless: bool = True) -> CheckResult:
    """Run the full search -> ID extraction -> results check for one username."""
    username = username.strip().lstrip("@")
    await _throttle()
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None

    async with async_playwright() as playwright:
        try:
            # Step 1: launch Chromium with a normal desktop context.
            # (through this batch's proxy, if any are configured)
            proxy = _current_proxy()
            browser = await playwright.chromium.launch(headless=headless, **({"proxy": proxy} if proxy else {}))
            context = await browser.new_context(
                locale="en-US",
                viewport={"width": 1366, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()

            # Step 2: open the Branded Content Library with target + date window.
            logger.info("Opening Ads Library")
            await page.goto(_library_url(), wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            await _dismiss_consent(page)

            # Step 3: wait for the search input.
            search = await _find_search_input(page)

            # Steps 4-5: clear the box and type the username like a user would.
            logger.info("Searching username @%s", username)
            await search.click()
            await search.fill("")
            await search.press_sequentially(username, delay=TYPE_DELAY_MS)

            # Steps 6-7: wait for suggestions and pull the numeric Instagram ID.
            instagram_id = await _extract_instagram_id(page, username)
            if instagram_id is None:
                return CheckResult(username=username, instagram_id=None, result_count=0)
            logger.info("Extracted Instagram ID %s", instagram_id)

            # Steps 8-9: open the results page for that account.
            results_url = _library_url(id=instagram_id, query=username)
            logger.info("Opening branded content page %s", results_url)
            await page.goto(results_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
            await _dismiss_consent(page)

            # Step 10: let the results load (network quiet), tolerating
            # long-polling connections that never go fully idle.
            try:
                await page.wait_for_load_state("networkidle", timeout=RESULTS_SETTLE_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                logger.info("Network never went idle — continuing with what has rendered")
            await page.wait_for_timeout(2_000)

            # Step 11: detect branded-content results.
            logger.info("Checking sponsorship results")
            count = await _count_results(page)
            return CheckResult(username=username, instagram_id=instagram_id, result_count=count)

        except PlaywrightTimeoutError as exc:
            await _save_debug_screenshot(page, username)
            raise CheckError(f"Timed out: {exc}") from exc
        except CheckError:
            await _save_debug_screenshot(page, username)
            raise
        finally:
            # Always release browser resources, even on failure.
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()


def _print_result(result: CheckResult) -> None:
    if result.found:
        print("FOUND sponsorships")
        print(f"Username: {result.username}")
        print(f"Instagram ID: {result.instagram_id}")
        print(f"Result Count: {result.result_count}")
    else:
        print("NOT FOUND")
        print(f"Username: {result.username}")
        print(f"Instagram ID: {result.instagram_id or 'not found in search dropdown'}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Check Meta's Branded Content Library for an Instagram account.")
    parser.add_argument("--username", default=TARGET_USERNAME, help=f"Instagram username (default: {TARGET_USERNAME}).")
    parser.add_argument("--headed", action="store_true", help="Show the browser window (for debugging).")
    args = parser.parse_args()

    try:
        result = asyncio.run(check_branded_content(args.username, headless=not args.headed))
    except CheckError as exc:
        logger.error("%s", exc)
        return 1
    except Exception:  # noqa: BLE001 — report any unexpected browser failure
        logger.exception("Unexpected error while checking @%s", args.username)
        return 1

    _print_result(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
