import logging
from apify_client import ApifyClient
from apify_client.errors import ApifyApiError
from config import APIFY_TOKEN

logger = logging.getLogger(__name__)


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

    Raises ApifyQuotaExceeded (instead of swallowing it like other errors)
    when the account has hit its monthly usage hard limit — see that
    class's docstring for why this one isn't just logged-and-skipped.
    """
    prefix = f"{label}: " if label else ""
    client = ApifyClient(APIFY_TOKEN)
    try:
        run = client.actor(actor_id).call(run_input=run_input)
        status = _run_field(run, "status", "status")
        if require_success and status != "SUCCEEDED":
            logger.error("%sApify actor failed — status: %s", prefix, status)
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
