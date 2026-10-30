import contextvars
import logging
import os
from contextlib import contextmanager

from apify_client import ApifyClient
from apify_client.errors import ApifyApiError

from config import APIFY_TOKEN

logger = logging.getLogger(__name__)

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
