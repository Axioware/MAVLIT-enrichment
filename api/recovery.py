"""
api/recovery.py

Startup recovery of work that a restart interrupted. Description and AI-ranking jobs run inside the API process: when it stops, a creator's
row is left as 'queued' or 'running' and nothing would ever finish it (the status endpoints keep returning the dead job, and saving the profile
is refused with 409 while a description is pending). At startup these are marked 'failed' with a plain message, and the in-flight pitch locks
(feature_locks) are cleared.

THIS IS ONLY SAFE WITH A SINGLE API WORKER PROCESS (the case today: one uvicorn worker). With more than one worker, a starting worker would
fail jobs that another, still running worker is working on: change this to fail only rows older than this process's start time, or run it
once outside the workers, before raising the worker count.
"""

import logging

from sqlalchemy import delete, update

from pipeline.db import CreatorProfile, FeatureLock, SessionLocal

logger = logging.getLogger(__name__)

RESTART_MESSAGE = "The server restarted, please try again"
_PENDING = ("queued", "running")
# (status column, error column)
_JOB_COLUMNS = (
    ("instagram_description_status", "instagram_description_error"),
    ("youtube_description_status", "youtube_description_error"),
    ("llm_ranking_status", "llm_ranking_error"),         # v2
    ("llm_v3_ranking_status", "llm_v3_ranking_error"),   # v3
)


def recover_stuck_jobs(session_factory=None) -> dict[str, int]:
    """Mark every pending description / ranking job failed and delete all feature_locks. Returns how many rows each step changed."""
    changed: dict[str, int] = {}
    with (session_factory or SessionLocal)() as db:
        for status_name, error_name in _JOB_COLUMNS:
            status, error = getattr(CreatorProfile, status_name), getattr(CreatorProfile, error_name)
            result = db.execute(update(CreatorProfile).where(status.in_(_PENDING)).values({status_name: "failed", error_name: RESTART_MESSAGE}))
            changed[status_name] = result.rowcount or 0
        changed["feature_locks"] = db.execute(delete(FeatureLock)).rowcount or 0
        db.commit()
    if any(changed.values()):
        logger.warning("Startup recovery after a restart: %s", {k: v for k, v in changed.items() if v})
    return changed
