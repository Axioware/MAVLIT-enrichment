"""
api/login_limits.py

Failed-login lockout kept in the database (table login_failures), so it holds across restarts and processes.

  * an email address is locked after MAX_FAILURES_PER_EMAIL failures within WINDOW_MINUTES (that user can try again when the oldest
    of them is WINDOW_MINUTES old);
  * a client address is locked after MAX_FAILURES_PER_IP failures within the same window (higher: users can share an address).

A locked login answers 429 without checking the password, so a correct guess during a lockout does not help. A successful login clears
that email's failures. The email is stored only as a SHA-256 hash.
"""

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func
from sqlalchemy.orm import Session

from pipeline.db import LoginFailure

MAX_FAILURES_PER_EMAIL = 5
MAX_FAILURES_PER_IP = 30
WINDOW_MINUTES = 15
KEEP_DAYS = 1


def email_hash(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def locked_for_seconds(db: Session, email: str, ip: str) -> int:
    """0 when the login may proceed, else the seconds until the lockout ends."""
    since = _now() - timedelta(minutes=WINDOW_MINUTES)
    wait = 0
    for column, value, limit in ((LoginFailure.email_hash, email_hash(email), MAX_FAILURES_PER_EMAIL), (LoginFailure.ip, ip, MAX_FAILURES_PER_IP)):
        times = [t for (t,) in db.query(LoginFailure.created_at).filter(column == value, LoginFailure.created_at > since)
                 .order_by(LoginFailure.created_at.desc()).limit(limit).all()]
        if len(times) >= limit:   # locked until the limit-th most recent failure leaves the window
            wait = max(wait, int((times[-1] + timedelta(minutes=WINDOW_MINUTES) - _now()).total_seconds()) + 1)
    return max(wait, 0)


def hits_locked_for_seconds(db: Session, key: str, limit: int, window_minutes: int) -> int:
    """For an action limited to `limit` uses per `window_minutes` under a private key (e.g. "logout-all:<user id>"): 0 if allowed, else the wait."""
    since = _now() - timedelta(minutes=window_minutes)
    times = [t for (t,) in db.query(LoginFailure.created_at).filter(LoginFailure.email_hash == email_hash(key), LoginFailure.created_at > since)
             .order_by(LoginFailure.created_at.desc()).limit(limit).all()]
    if len(times) < limit:
        return 0
    return max(int((times[-1] + timedelta(minutes=window_minutes) - _now()).total_seconds()) + 1, 1)


def record_failure(db: Session, email: str, ip: str) -> None:
    db.add(LoginFailure(email_hash=email_hash(email), ip=ip))
    db.execute(delete(LoginFailure).where(LoginFailure.created_at < _now() - timedelta(days=KEEP_DAYS)))
    db.commit()


def clear_failures(db: Session, email: str) -> None:
    if db.query(func.count()).select_from(LoginFailure).filter(LoginFailure.email_hash == email_hash(email)).scalar():
        db.execute(delete(LoginFailure).where(LoginFailure.email_hash == email_hash(email)))
        db.commit()
