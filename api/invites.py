"""
The early-access invitation link: GET /auth/invite/{token} says whether the link can still be used (and for which email), POST
/auth/invite/accept lets the invited person choose a password, which creates their account. Public (no login).

The token is 256 random bits and only its SHA-256 is stored (pipeline/early_access_invites.py makes it). A link works once: accepting it
sets used_at, and from then on the page sends the visitor to the app / sign-in instead.
"""
import hashlib
import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api import attempt_limits
from api.auth import MIN_PASSWORD_LENGTH
from pipeline.db import AccountInvite, CreatorProfile, get_db
from pipeline.helpers.passwords import hash_password

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/invite", tags=["invites"])


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _find(db: Session, token: str, lock: bool = False) -> AccountInvite | None:
    token = (token or "").strip()
    if not token or len(token) > 200:
        return None
    query = db.query(AccountInvite).filter(AccountInvite.token_hash == hash_token(token))
    row = (query.with_for_update() if lock else query).first()
    return row if row is not None and hmac.compare_digest(row.token_hash, hash_token(token)) else None


def _status(db: Session, invite: AccountInvite | None) -> str:
    """valid | used | expired | invalid. An invite whose email already has an account counts as used."""
    if invite is None:
        return "invalid"
    if invite.used_at is not None:
        return "used"
    if db.query(CreatorProfile.id).filter(CreatorProfile.email == invite.email).first() is not None:
        return "used"
    if invite.expires_at <= datetime.now(timezone.utc):
        return "expired"
    return "valid"


@router.get("/{token}")
def invite_status(token: str, request: Request, db: Session = Depends(get_db)):
    """{"status": valid | used | expired | invalid, "email": ...}. The email is only given for a link that can still be used."""
    attempt_limits.check("invite", request)
    invite = _find(db, token)
    status = _status(db, invite)
    if status == "invalid":
        attempt_limits.failed("invite", request)   # guessing tokens locks the address out after a few tries
    return {"status": status, "email": invite.email if status == "valid" else None}


class AcceptInviteRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


@router.post("/accept")
def accept_invite(body: AcceptInviteRequest, request: Request, db: Session = Depends(get_db)):
    """Creates the account with the chosen password and uses up the link. The page signs the person in afterwards with the normal login."""
    attempt_limits.check("invite", request)
    if len(body.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=422, detail=f"Choose a password of at least {MIN_PASSWORD_LENGTH} characters.")
    try:
        password_hash = hash_password(body.password)
    except ValueError as exc:   # longer than bcrypt's 72 bytes
        raise HTTPException(status_code=422, detail=str(exc))

    invite = _find(db, body.token, lock=True)   # FOR UPDATE: two clicks at once cannot both create the account
    status = _status(db, invite)
    if status != "valid":
        db.rollback()
        if status == "invalid":
            attempt_limits.failed("invite", request)
        raise HTTPException(status_code=410, detail={"status": status, "message": "This link can no longer be used."})
    try:
        db.add(CreatorProfile(email=invite.email, password_hash=password_hash, is_active=True))
        invite.used_at = datetime.now(timezone.utc)
        db.commit()
    except IntegrityError:   # an account with this email appeared meanwhile
        db.rollback()
        raise HTTPException(status_code=410, detail={"status": "used", "message": "This link can no longer be used."})
    logger.info("Early-access invite accepted: account created for invite_id=%s", invite.id)
    return {"status": "ok", "email": invite.email}
