import logging

from apify_client import ApifyClient

from config import APIFY_TOKEN

logger = logging.getLogger(__name__)


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
    """
    prefix = f"{label}: " if label else ""
    client = ApifyClient(APIFY_TOKEN)
    try:
        run = client.actor(actor_id).call(run_input=run_input)
        if require_success and run.status != "SUCCEEDED":
            logger.error("%sApify actor failed — status: %s", prefix, run.status)
            return None
        items = list(client.dataset(run.default_dataset_id).iterate_items())
        logger.info("%s%d items from Apify", prefix, len(items))
        return items
    except Exception as exc:
        logger.error("%sApify actor %s failed — %s", prefix, actor_id, exc)
        return None if require_success else []
