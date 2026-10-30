"""
api/attempt_limits.py

Attempt limit and lockout for the shared passkeys: the /admin login form and the operator Bearer check. It reuses the database-backed
store of api/login_limits.py (table login_failures), keyed by a SHA-256 of "<scope>:<client address>", so a lockout holds across restarts and
processes and one client cannot lock another one out. Five failures within 15 minutes lock that client for the rest of the window (HTTP 429
with Retry-After); the passkey is not even compared while locked.

If the store itself cannot be reached the check is skipped with a warning: the passkey is still required, only the rate limit is lost.
"""

import logging

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from api import login_limits
from api.client_ip import client_ip
from pipeline.db import SessionLocal

logger = logging.getLogger(__name__)


def _key(scope: str, request) -> tuple[str, str]:
    ip = client_ip(request)
    return f"{scope}:{ip}", ip


def check(scope: str, request) -> None:
    """Raise HTTP 429 while this client is locked out of `scope` ("admin" or "operator")."""
    key, ip = _key(scope, request)
    try:
        with SessionLocal() as db:
            wait = login_limits.locked_for_seconds(db, key, ip)
    except SQLAlchemyError:
        logger.warning("%s attempt limit unavailable (database not reachable): not enforced", scope)
        return
    if wait:
        minutes = max(1, -(-wait // 60))
        raise HTTPException(status_code=429, detail=f"Too many failed attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.",
                            headers={"Retry-After": str(wait)})


def failed(scope: str, request) -> None:
    key, ip = _key(scope, request)
    try:
        with SessionLocal() as db:
            login_limits.record_failure(db, key, ip)
    except SQLAlchemyError:
        logger.warning("%s attempt limit unavailable (database not reachable): failure not recorded", scope)


def succeeded(scope: str, request) -> None:
    key, _ = _key(scope, request)
    try:
        with SessionLocal() as db:
            login_limits.clear_failures(db, key)
    except SQLAlchemyError:
        pass
