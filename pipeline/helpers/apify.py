import contextvars
import logging
import os
import re
from contextlib import contextmanager

from apify_client import ApifyClient
from apify_client.errors import ApifyApiError

from config import APIFY_TOKEN

logger = logging.getLogger(__name__)

# The Instagram scraper (apify/instagram-scraper) every Instagram step uses. Its runs are recorded in apify_usage with an estimated cost.
INSTAGRAM_ACTOR_ID = "shu8hvrXbJbY3Eb9W"
_DEFAULT_INSTAGRAM_USD_PER_1000 = 2.30


def instagram_usd_per_1000() -> float:
    """Price of 1,000 results of the Instagram scraper in USD (APIFY_INSTAGRAM_USD_PER_1000, default 2.30)."""
    raw = (os.environ.get("APIFY_INSTAGRAM_USD_PER_1000") or "").strip()
    if not raw:
        return _DEFAULT_INSTAGRAM_USD_PER_1000
    try:
        value = float(raw)
        if value < 0:
            raise ValueError
        return value
    except ValueError:
        logger.warning("APIFY_INSTAGRAM_USD_PER_1000=%r is not a valid price: using %.2f", raw, _DEFAULT_INSTAGRAM_USD_PER_1000)
        return _DEFAULT_INSTAGRAM_USD_PER_1000


def instagram_cost_usd(results: int) -> float:
    """Estimated cost of an Instagram scraper run that returned `results` items: results x price / 1,000."""
    return round(max(int(results), 0) * instagram_usd_per_1000() / 1000, 6)


def _handle_from_input(run_input: dict, label: str) -> str:
    """The Instagram username a run was for (from its first profile URL), else the call's label."""
    for url in (run_input.get("directUrls") or [])[:1]:
        found = re.search(r"instagram\.com/([A-Za-z0-9._]+)/?$", str(url))
        if found and found.group(1) not in ("p", "reel", "tv"):
            return found.group(1)
    return label or "instagram"


def _record_instagram_run(actor_id: str, label: str, run_input: dict, results: int) -> None:
    """Log a finished Instagram scraper run in apify_usage (who it was for, how many results, the estimated cost). Never raises."""
    if actor_id != INSTAGRAM_ACTOR_ID:
        return
    try:
        from pipeline.helpers.gpt_llm import record_apify_scrape   # lazy: gpt_llm must not load with this module

        record_apify_scrape("instagram", _handle_from_input(run_input, label), int(run_input.get("resultsLimit") or 0), results,
                            actor_id=actor_id, label=label)
    except Exception:
        logger.warning("Apify usage: could not record the Instagram run %r", label, exc_info=True)

# Apify's own client defaults to waiting *indefinitely* for a run to finish
# (timeout_secs / wait_secs both default to None) — a stuck actor run (a
# real, observed failure mode for scraper actors) would then block the
# calling thread forever, holding open whatever DB transaction/connection
# was checked out just before this call. Both are set explicitly so a call
# always returns:
#   _RUN_TIMEOUT_SECS — server-side: Apify kills the run itself after this.
#   _WAIT_SECS        — client-side: how long we poll before giving up on
#                        it and returning, even if Apify hasn't killed the
#                        run yet. Kept slightly above _RUN_TIMEOUT_SECS so a
#                        normally-finishing run isn't cut off early here.
_RUN_TIMEOUT_SECS = 600
_WAIT_SECS = 630

# A person is waiting on a request-path run (adding a post, a pitch that reads linked posts, a profile description, an own-brand lookup), so
# those runs are cut off at APIFY_REQUEST_TIMEOUT_SECS (default 90) for both the server-side run limit and the client-side wait. Batch jobs
# keep the limits above. A request path opts in with `with request_path():` around its calls (a context variable: it covers the calls
# made on that thread, not worker threads started inside).
_request_limit: contextvars.ContextVar[int | None] = contextvars.ContextVar("apify_request_limit", default=None)


def request_timeout_secs() -> int:
    raw = (os.environ.get("APIFY_REQUEST_TIMEOUT_SECS") or "").strip()
    if not raw:
        return 90
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"APIFY_REQUEST_TIMEOUT_SECS must be a whole number of seconds, got {raw!r}")
    if value < 10:
        raise ValueError(f"APIFY_REQUEST_TIMEOUT_SECS must be >= 10, got {value}")
    return value


request_timeout_secs()   # a bad setting stops the application at start


@contextmanager
def request_path():
    token = _request_limit.set(request_timeout_secs())
    try:
        yield
    finally:
        _request_limit.reset(token)


def _limits() -> tuple[int, int]:
    """(run timeout, wait) in seconds for the call being made now."""
    limit = _request_limit.get()
    return (min(limit, _RUN_TIMEOUT_SECS), limit) if limit else (_RUN_TIMEOUT_SECS, _WAIT_SECS)


class ApifyQuotaExceeded(Exception):
    """
    Raised when the Apify account has hit its monthly usage hard limit.
    Unlike ordinary transient failures (blocked proxy, timeout, rate limit),
    this doesn't clear up on its own within a run — every subsequent call
    fails identically until the billing cycle resets or the plan is
    upgraded. Callers should let this propagate and stop the run early
    instead of retrying every remaining item against the same wall.
    """


def _run_field(run, dict_key: str, attr_key: str):
    """
    apify-client's actor().call() has been observed to return a plain dict
    on one environment (Python 3.10 here) and a typed, non-subscriptable
    Run-like object on another (Python 3.14 on a fresh server, same
    apify-client==2.5.1 and same declared dependencies — likely a
    difference in the impit HTTP backend across Python versions) — support
    both shapes rather than assuming one crashes the other.
    """
    if isinstance(run, dict):
        return run.get(dict_key)
    return getattr(run, attr_key, None)


def run_apify_actor(
    actor_id: str,
    run_input: dict,
    *,
    label: str = "",
    require_success: bool = False,
) -> list[dict] | None:
    """
    Run an Apify actor and return its dataset items.

    label           — prefix for log messages (e.g. "Instagram @nike")
    require_success — when True, returns None if actor status != SUCCEEDED
                      instead of []. Use this when the caller needs to
                      distinguish a failed run from an empty result set
                      (e.g. Twitter retry-on-failure logic).

    Returns [] on exception unless require_success=True, in which case
    returns None so the caller can skip marking the brand as processed.
    A run that doesn't finish within _WAIT_SECS is treated the same way
    (status will be non-terminal, e.g. "RUNNING") rather than blocking.

    Raises ApifyQuotaExceeded (instead of swallowing it like other errors)
    when the account has hit its monthly usage hard limit — see that
    class's docstring for why this one isn't just logged-and-skipped.
    """
    prefix = f"{label}: " if label else ""
    client = ApifyClient(APIFY_TOKEN)
    try:
        run_timeout, wait = _limits()
        run = client.actor(actor_id).call(
            run_input=run_input,
            timeout_secs=run_timeout,
            wait_secs=wait,
        )
        status = _run_field(run, "status", "status")
        if status != "SUCCEEDED":
            level = logger.error if require_success else logger.warning
            level("%sApify actor did not succeed — status: %s", prefix, status)
            if require_success:
                return None
        dataset_id = _run_field(run, "defaultDatasetId", "default_dataset_id")
        items = list(client.dataset(dataset_id).iterate_items())
        logger.info("%s%d items from Apify", prefix, len(items))
        _record_instagram_run(actor_id, label, run_input, len(items))
        return items
    except ApifyApiError as exc:
        if "usage hard limit" in (exc.message or "").lower():
            raise ApifyQuotaExceeded(exc.message) from exc
        logger.exception("%sApify actor %s failed", prefix, actor_id)
        return None if require_success else []
    except Exception:
        # .exception() (not .error()) so a full traceback lands in the logs —
        # a one-line summary wasn't enough to diagnose the dict-vs-object
        # Run shape difference above when it first showed up.
        logger.exception("%sApify actor %s failed", prefix, actor_id)
        return None if require_success else []
