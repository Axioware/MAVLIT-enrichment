"""
api/operator.py

Operator access: the credential for the endpoints that start pipeline jobs or write in bulk (/seed, /enrich/signals,
/score/brands, /content-creator-re/bulk-add and their status routes) and for the /admin interface (SQLAdmin).

It is the existing ADMIN_PASSKEY (config.py), a single shared secret held by the operators, not a creator account. API calls send it as
    Authorization: Bearer <ADMIN_PASSKEY>
and the /admin login form takes it in its Password field. Everything fails closed: a missing, blank (whitespace only), malformed or wrong
credential is refused, and when ADMIN_PASSKEY itself is not set (or blank) nobody can get in, whatever they send. The comparison is constant
time. Nothing here logs the passkey or what was sent.

Local development only: ADMIN_ALLOW_UNAUTHENTICATED=1 leaves /admin open when no passkey is set. It is ignored when ENVIRONMENT=production, and it
never opens the operator endpoints.
"""

import hmac
import logging
import os

from fastapi import Header, HTTPException, Request

import config
from api import attempt_limits

logger = logging.getLogger(__name__)

MIN_RECOMMENDED_LENGTH = 12   # only warned about at startup; shorter passkeys keep working


def passkey() -> str | None:
    """The configured passkey, or None when it is missing or blank. Read at call time (config.ADMIN_PASSKEY)."""
    value = config.ADMIN_PASSKEY
    return value if isinstance(value, str) and value.strip() else None


def operator_configured() -> bool:
    return passkey() is not None


def passkey_matches(candidate: str | None) -> bool:
    key = passkey()
    if key is None or not candidate or not candidate.strip():
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), key.encode("utf-8"))


def operator_key() -> str | None:
    """The Bearer credential of the operator endpoints: OPERATOR_PASSKEY when it is set, otherwise (as before) ADMIN_PASSKEY."""
    value = getattr(config, "OPERATOR_PASSKEY", "")
    if isinstance(value, str) and value.strip():
        return value
    return passkey()


def operator_key_matches(candidate: str | None) -> bool:
    key = operator_key()
    if key is None or not candidate or not candidate.strip():
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), key.encode("utf-8"))


def allow_unauthenticated_admin() -> bool:
    """Development escape hatch for /admin only; never honoured in production."""
    flag = os.getenv("ADMIN_ALLOW_UNAUTHENTICATED", "").strip().lower() in ("1", "true", "yes", "on")
    return flag and not config.IS_PRODUCTION


def require_operator(request: Request, authorization: str | None = Header(default=None)) -> None:
    """
    FastAPI dependency for operator endpoints: Authorization: Bearer <OPERATOR_PASSKEY, or ADMIN_PASSKEY when that is not set>, else 401
    (403 when no passkey is configured). Five failures within 15 minutes lock the client out (429, api/attempt_limits.py).
    """
    if operator_key() is None:
        logger.warning("Operator endpoint refused: no operator passkey is configured, so operator access is disabled")
        raise HTTPException(status_code=403, detail="Operator access is not enabled")
    attempt_limits.check("operator", request)
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not operator_key_matches(token.strip()):
        attempt_limits.failed("operator", request)
        raise HTTPException(status_code=401, detail="Operator credentials required", headers={"WWW-Authenticate": "Bearer"})
