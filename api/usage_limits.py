"""
api/usage_limits.py

Per-user cost controls for the features that spend LLM or Apify money, kept in the database (tables feature_usage and feature_locks), so
they hold across restarts and processes, and one user's usage never counts against another's.

  * Daily caps (UTC day), one setting per feature; 0 turns that feature off (HTTP 403). Over the cap: HTTP 429 with Retry-After (seconds
    to the next UTC midnight). The count is taken BEFORE the LLM / Apify call, in one atomic statement, and every attempt counts.
        PITCHES_PER_DAY 30 | RATE_INTELLIGENCE_PER_DAY 20 | CONTRACT_ADVICE_PER_DAY 10 | MATCH_REFRESH_PER_DAY 10
        OWN_BRANDS_PER_DAY 10 | DESCRIPTION_GENERATIONS_PER_DAY 20
  * One in-flight pitch (or pitch stream) per user: HTTP 409 while another is running. A lock row older than PITCH_LOCK_STALE_SECONDS
    (default 300, longer than the slowest pitch) is taken over, so a crashed process cannot block a user for long.
  * SAVED_BRANDS_MAX (default 500) caps how many brands one user can save.

A bad value in an environment variable stops the application at start (the module checks them all when imported).
"""

import os
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from pipeline.db import FeatureLock, FeatureUsage

# feature -> (environment variable, default, what the user sees in the messages)
DAILY_CAPS = {
    "pitches":                ("PITCHES_PER_DAY", 30, "pitches"),
    "rate_intelligence":      ("RATE_INTELLIGENCE_PER_DAY", 20, "rate estimates"),
    "contract_advice":        ("CONTRACT_ADVICE_PER_DAY", 10, "contract reviews"),
    "match_refresh":          ("MATCH_REFRESH_PER_DAY", 10, "match refreshes"),
    "own_brands":             ("OWN_BRANDS_PER_DAY", 10, "own-brand lookups"),
    "description_generation": ("DESCRIPTION_GENERATIONS_PER_DAY", 20, "description generations"),
}
PITCH_LOCK = "pitch"


def _int_setting(name: str, default: int, minimum: int = 0) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def daily_cap(feature: str) -> int:
    name, default, _ = DAILY_CAPS[feature]
    return _int_setting(name, default)


def lock_stale_seconds() -> int:
    return _int_setting("PITCH_LOCK_STALE_SECONDS", 300, minimum=30)


def saved_brands_max() -> int:
    return _int_setting("SAVED_BRANDS_MAX", 500)


def check_settings() -> None:
    for feature in DAILY_CAPS:
        daily_cap(feature)
    lock_stale_seconds()
    saved_brands_max()


check_settings()   # fail at startup, not on the first request, when a setting is bad


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _seconds_to_midnight() -> int:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((tomorrow - now).total_seconds()), 1)


def consume(db: Session, creator_id: int, feature: str) -> None:
    """Count one use of `feature` today for this creator, or raise HTTP 429 (cap used up) / 403 (feature turned off). Commits."""
    cap = daily_cap(feature)
    label = DAILY_CAPS[feature][2]
    if cap == 0:
        raise HTTPException(status_code=403, detail=f"{label.capitalize()} are turned off right now.")
    stmt = (
        pg_insert(FeatureUsage)
        .values(creator_profile_id=creator_id, feature=feature, used_on=_today(), uses=1)
        .on_conflict_do_update(index_elements=["creator_profile_id", "feature", "used_on"],
                               set_={"uses": FeatureUsage.uses + 1}, where=FeatureUsage.uses < cap)
        .returning(FeatureUsage.uses)
    )
    if db.execute(stmt).first() is None:
        db.rollback()
        raise HTTPException(
            status_code=429,
            detail=f"You have used your {cap} {label} for today, which is the daily limit. Try again tomorrow.",
            headers={"Retry-After": str(_seconds_to_midnight())},
        )
    db.commit()


def acquire_lock(db: Session, creator_id: int, feature: str = PITCH_LOCK) -> None:
    """Take this creator's in-flight lock for `feature`, or raise HTTP 409 when another request holds it. Commits."""
    stale = timedelta(seconds=lock_stale_seconds())
    stmt = (
        pg_insert(FeatureLock)
        .values(creator_profile_id=creator_id, feature=feature)
        .on_conflict_do_update(index_elements=["creator_profile_id", "feature"], set_={"started_at": func.now()},
                               where=FeatureLock.started_at < func.now() - stale)
        .returning(FeatureLock.creator_profile_id)
    )
    got = db.execute(stmt).first()
    db.commit()
    if got is None:
        raise HTTPException(status_code=409, detail="Another pitch is already being written for you. Wait for it to finish, then try again.")


def release_lock(bind, creator_id: int, feature: str = PITCH_LOCK) -> None:
    """Free the lock in its own short session (the request's session may be closed by now). Safe to call twice."""
    try:
        with Session(bind) as session:
            session.execute(delete(FeatureLock).where(FeatureLock.creator_profile_id == creator_id, FeatureLock.feature == feature))
            session.commit()
    except Exception:   # a failed release must never turn a finished request into an error: the stale limit frees the row
        import logging
        logging.getLogger(__name__).warning("Could not release the %s lock of creator %s", feature, creator_id, exc_info=True)
