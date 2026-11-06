"""
"Forgot password?": POST /auth/forgot-password emails a single-use link to a registered email and says so; for an email with no account
it sends nothing and says the account is not registered (so the visitor can correct the address or join the waiting list). GET /auth/password-reset/{token} says whether the link still works, POST /auth/password-reset sets the new password and ends
every session of that account. Public (no login). Only the SHA-256 of the token is stored (table password_reset_tokens).
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from api import attempt_limits, login_limits
from api.client_ip import client_ip
from pipeline.db import CreatorProfile, PasswordResetToken, get_db
from pipeline.helpers.passwords import hash_password
from pipeline.mailer import send_by_smtp
from pipeline.password_reset_email import build_reset_message

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["password-reset"])

RESET_MINUTES = 60                 # how long a reset link works
MIN_RESET_PASSWORD_LENGTH = 6      # same rule as the early-access password page
_PER_EMAIL, _PER_IP, _WINDOW_MINUTES = 3, 10, 60   # emails asked for per address / per IP per hour
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _app_url() -> str:
    return (os.environ.get("APP_BASE_URL") or "https://app.mavlit.com").strip().rstrip("/")


def _send(email: str, link: str) -> None:
    try:
        send_by_smtp([build_reset_message(email, link, RESET_MINUTES, (os.environ.get("MAIL_FROM") or "").strip() or None)])
    except Exception:   # the visitor is told the same thing either way; the operator reads the log
        logger.exception("Could not send the password-reset email")


class ForgotPasswordRequest(BaseModel):
    email: str = Field(min_length=1, max_length=254)


@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordRequest, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    """
    {"status": ...}: "ok" (a link was emailed, in the background), "not_registered" (no account with this email: nothing is sent),
    "inactive" (the account is deactivated: nothing is sent), "limited" (this address was mailed 3 times in the last hour: nothing more
    is sent), "invalid_email". Too many requests from one address answer HTTP 429.
    """
    email, ip = body.email.strip().lower(), client_ip(request)
    if not _EMAIL.match(email):
        return {"status": "invalid_email"}
    wait = login_limits.hits_locked_for_seconds(db, f"forgot-ip:{ip}", _PER_IP, _WINDOW_MINUTES)
    if wait:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.", headers={"Retry-After": str(wait)})
    login_limits.record_failure(db, f"forgot-ip:{ip}", ip)   # commits; every request counts
    user = db.query(CreatorProfile).filter(CreatorProfile.email == email).first()
    if user is None:
        return {"status": "not_registered"}
    if not user.is_active:
        return {"status": "inactive"}
    if login_limits.hits_locked_for_seconds(db, f"forgot:{email}", _PER_EMAIL, _WINDOW_MINUTES):
        return {"status": "limited"}                          # this address was mailed a lot already: nothing more is sent
    login_limits.record_failure(db, f"forgot:{email}", ip)
    token = secrets.token_urlsafe(32)
    values = {"email": email, "token_hash": _hash(token), "expires_at": datetime.now(timezone.utc) + timedelta(minutes=RESET_MINUTES), "used_at": None}
    db.execute(pg_insert(PasswordResetToken).values(**values).on_conflict_do_update(
        index_elements=[PasswordResetToken.email], set_={k: v for k, v in values.items() if k != "email"}))
    db.commit()
    background.add_task(_send, email, f"{_app_url()}/reset-password?token={token}")
    return {"status": "ok"}


def _find(db: Session, token: str, lock: bool = False) -> PasswordResetToken | None:
    token = (token or "").strip()
    if not token or len(token) > 200:
        return None
    query = db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == _hash(token))
    row = (query.with_for_update() if lock else query).first()
    return row if row is not None and hmac.compare_digest(row.token_hash, _hash(token)) else None


def _status(db: Session, row: PasswordResetToken | None) -> tuple[str, CreatorProfile | None]:
    """(valid | used | expired | invalid, the account). A link for an account that no longer exists or is deactivated is invalid."""
    if row is None:
        return "invalid", None
    user = db.query(CreatorProfile).filter(CreatorProfile.email == row.email).first()
    if user is None or not user.is_active:
        return "invalid", None
    if row.used_at is not None:
        return "used", user
    if row.expires_at <= datetime.now(timezone.utc):
        return "expired", user
    return "valid", user


@router.get("/password-reset/{token}")
def reset_status(token: str, request: Request, db: Session = Depends(get_db)):
    """{"status": valid | used | expired | invalid, "email": ...}. The email is only given for a link that can still be used."""
    attempt_limits.check("reset", request)
    row = _find(db, token)
    status, _user = _status(db, row)
    if status == "invalid":
        attempt_limits.failed("reset", request)
    return {"status": status, "email": row.email if status == "valid" else None}


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


@router.post("/password-reset")
def reset_password(body: ResetPasswordRequest, request: Request, db: Session = Depends(get_db)):
    """Sets the new password, uses up the link, and signs the account out everywhere. The page signs the person in with the normal login."""
    attempt_limits.check("reset", request)
    if len(body.password) < MIN_RESET_PASSWORD_LENGTH:
        raise HTTPException(status_code=422, detail=f"Choose a password of at least {MIN_RESET_PASSWORD_LENGTH} characters.")
    try:
        new_hash = hash_password(body.password)
    except ValueError as exc:   # longer than bcrypt's 72 bytes
        raise HTTPException(status_code=422, detail=str(exc))

    row = _find(db, body.token, lock=True)   # FOR UPDATE: two uses at once cannot both succeed
    status, user = _status(db, row)
    if status != "valid":
        db.rollback()
        if status == "invalid":
            attempt_limits.failed("reset", request)
        raise HTTPException(status_code=410, detail={"status": status, "message": "This link can no longer be used."})
    user.password_hash = new_hash
    user.token_version = int(user.token_version or 0) + 1   # every login issued before stops working
    if hasattr(user, "must_change_password"):
        user.must_change_password = False
    row.used_at = datetime.now(timezone.utc)
    db.commit()
    login_limits.clear_failures(db, user.email)              # a lockout from earlier wrong passwords must not block the new one
    logger.info("Password reset completed for creator_id=%s", user.id)
    return {"status": "ok", "email": user.email}
