import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.orm import Session

from api import login_limits
from api.client_ip import client_ip
from api.schemas import CreatorProfileResponse, profile_to_response
import config
from config import JWT_SECRET
from pipeline.db import CreatorProfile, get_db
from pipeline.helpers.passwords import hash_password, needs_rehash, verify_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_ALGORITHM  = "HS256"


def token_lifetime() -> timedelta:
    """How long a login lasts (config.ACCESS_TOKEN_DAYS): used for both the JWT exp and the cookie max_age, so they cannot drift apart."""
    return timedelta(days=config.ACCESS_TOKEN_DAYS)

MIN_PASSWORD_LENGTH = 12

# Routes a creator who must change their password (must_change_password) may still call.
_PASSWORD_CHANGE_ALLOWED = {"/auth/me", "/auth/change-password", "/auth/logout", "/auth/logout-all"}


def _cookie_attributes() -> dict:
    """
    The attributes of the login cookie, read from config at call time. The delete uses exactly the same ones (a browser only removes a cookie
    when name, domain and path match). SameSite comes from COOKIE_SAMESITE (default lax); secure is on in production.
    """
    return {"httponly": True, "secure": config.IS_PRODUCTION, "samesite": config.COOKIE_SAMESITE, "domain": config.COOKIE_DOMAIN, "path": "/"}


def _set_login_cookie(response: Response, token: str) -> None:
    response.set_cookie("access_token", token, max_age=int(token_lifetime().total_seconds()), **_cookie_attributes())


def _clear_login_cookie(response: Response) -> None:
    response.delete_cookie("access_token", **_cookie_attributes())


class LoginRequest(BaseModel):
    email: str
    password: str


#  JWT helpers

def _make_jwt(user_id: int, email: str, token_version: int = 0) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": str(user_id), "email": email, "iat": now, "exp": now + token_lifetime(), "tv": int(token_version or 0)},
        JWT_SECRET,
        algorithm=_ALGORITHM,
    )


def _decode_jwt(token: str) -> dict:
    """The claims of a valid token. Only HS256 is accepted ('none' and every other algorithm are refused), and exp, sub and iat must be present."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[_ALGORITHM], options={"require_exp": True, "require_sub": True, "require_iat": True})
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def _user_for_claims(db: Session, payload: dict) -> CreatorProfile:
    """The active user the claims belong to, if the token has not been revoked; otherwise HTTP 401 (never a 500 for a malformed claim)."""
    try:
        user_id = int(payload["sub"])
        token_version = int(payload.get("tv", 0) or 0)   # a token from before revocation existed counts as version 0
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = db.query(CreatorProfile).filter(CreatorProfile.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    if token_version != int(user.token_version or 0):
        raise HTTPException(status_code=401, detail="Your session has ended. Please sign in again.")
    return user


#  Auth dependency

def get_current_user(request: Request, db: Session = Depends(get_db)) -> CreatorProfile:
    """FastAPI dependency — returns the logged-in CreatorProfile or raises 401."""
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = _user_for_claims(db, _decode_jwt(token))
    if user.must_change_password and request.url.path not in _PASSWORD_CHANGE_ALLOWED:
        raise HTTPException(status_code=403, detail={"code": "password_change_required", "message": "You must set a new password before you can continue."})
    return user


def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> CreatorProfile | None:
    """Like get_current_user, but returns None rather than raising when unauthenticated."""
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        return _user_for_claims(db, _decode_jwt(token))
    except HTTPException:
        return None


def is_profile_complete(profile: CreatorProfile | dict | None) -> bool:
    """True when a creator has completed the required onboarding profile."""
    if profile is None:
        return False

    if isinstance(profile, dict):
        full_name = str(profile.get("full_name") or "").strip()
        age = profile.get("age")
        gender = str(profile.get("gender") or "").strip()
        primary_platform = str(profile.get("primary_platform") or "").strip()
        follower_count = profile.get("follower_count")
        content_niche = str(profile.get("content_niche") or "").strip()
        instagram_handle = str(profile.get("instagram_handle") or "").strip()
        instagram_followers = profile.get("instagram_followers")
        youtube_channel_name = str(profile.get("youtube_channel_name") or "").strip()
        youtube_subscribers = profile.get("youtube_subscribers")
        instagram_description = str(profile.get("instagram_description") or "").strip()
        youtube_description = str(profile.get("youtube_description") or "").strip()
    else:
        full_name = (profile.full_name or "").strip()
        age = profile.age
        gender = (profile.gender or "").strip()
        primary_platform = (profile.primary_platform or "").strip()
        follower_count = profile.follower_count
        content_niche = (profile.content_niche or "").strip()
        instagram_handle = (profile.instagram_handle or "").strip()
        instagram_followers = profile.instagram_followers
        youtube_channel_name = (profile.youtube_channel_name or "").strip()
        youtube_subscribers = profile.youtube_subscribers
        instagram_description = (profile.instagram_description or "").strip()
        youtube_description = (profile.youtube_description or "").strip()

    if not full_name or age is None or not gender:
        return False

    selected_platforms = [p.strip().lower() for p in primary_platform.split(",") if p.strip()]
    if not selected_platforms:
        return False

    if not follower_count or follower_count <= 0:
        return False

    if not content_niche:
        return False

    description_text = instagram_description or youtube_description
    if not description_text:
        return False

    if "instagram" in selected_platforms:
        if not instagram_handle or instagram_followers is None or instagram_followers <= 0:
            return False

    if "youtube" in selected_platforms:
        if not youtube_channel_name or youtube_subscribers is None or youtube_subscribers <= 0:
            return False

    return True


def get_completed_user(current_user: CreatorProfile = Depends(get_current_user)) -> CreatorProfile:
    """Require authentication and a completed creator profile."""
    if not is_profile_complete(current_user):
        raise HTTPException(status_code=403, detail="Complete your creator profile before accessing the application")
    return current_user


#  Routes

_dummy_hash: str | None = None


def _dummy_password_hash() -> str:
    """A real bcrypt hash of a throwaway value, so an unknown email costs the same time as a wrong password."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password("not-a-real-password")
    return _dummy_hash


@router.post("/login", response_model=CreatorProfileResponse)
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    ip = client_ip(request)

    wait = login_limits.locked_for_seconds(db, email, ip)
    if wait:   # locked: the password is not even checked
        minutes = max(1, -(-wait // 60))
        raise HTTPException(status_code=429, detail=f"Too many failed sign-in attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.",
                            headers={"Retry-After": str(wait)})

    user = db.query(CreatorProfile).filter(CreatorProfile.email == email).first()
    # Generic error either way — don't reveal whether the email exists. An unknown email still runs one bcrypt check, so the time is the same.
    stored_hash = user.password_hash if user and user.password_hash else None
    password_ok = verify_password(body.password, stored_hash or _dummy_password_hash())
    if not (user and stored_hash and password_ok):
        login_limits.record_failure(db, email, ip)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account has been deactivated")

    login_limits.clear_failures(db, email)
    if needs_rehash(stored_hash):   # the stored hash was made with a lower cost than today's: upgrade it now that the password is known
        user.password_hash = hash_password(body.password)
        db.commit()
    _set_login_cookie(response, _make_jwt(user.id, user.email, user.token_version))
    return profile_to_response(user)


@router.get("/me")
def me(current_user: CreatorProfile = Depends(get_current_user)):
    """Return the logged-in user's profile."""
    return profile_to_response(current_user)


@router.post("/logout")
def logout(response: Response):
    """Clear the JWT cookie on this browser (other sessions stay valid: see /auth/logout-all)."""
    # Must match every attribute it was set with, or some browsers treat this as a different cookie and leave the real one intact.
    _clear_login_cookie(response)
    return {"message": "Logged out"}


def _bump_token_version(db: Session, user: CreatorProfile) -> int:
    """token_version + 1 in the database (one atomic UPDATE); every token issued before is refused from now on."""
    new_version = db.execute(update(CreatorProfile).where(CreatorProfile.id == user.id).values(token_version=CreatorProfile.token_version + 1)
                             .returning(CreatorProfile.token_version)).scalar_one()
    user.token_version = new_version
    return new_version


LOGOUT_ALL_LIMIT = 10          # uses per user ...
LOGOUT_ALL_WINDOW_MINUTES = 60  # ... per hour (every call counts, kept in login_failures under a private key)


@router.post("/logout-all")
def logout_all(request: Request, response: Response, db: Session = Depends(get_db), current_user: CreatorProfile = Depends(get_current_user)):
    """Sign this account out everywhere: every token issued so far (on every device) stops working. Returns no token and no user data."""
    key = f"logout-all:{current_user.id}"
    wait = login_limits.hits_locked_for_seconds(db, key, LOGOUT_ALL_LIMIT, LOGOUT_ALL_WINDOW_MINUTES)
    if wait:
        raise HTTPException(status_code=429, detail="Too many attempts, try again later.", headers={"Retry-After": str(wait)})
    login_limits.record_failure(db, key, client_ip(request))   # commits
    _bump_token_version(db, current_user)
    db.commit()
    _clear_login_cookie(response)
    return {"status": "ok"}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=200)


@router.post("/change-password", response_model=CreatorProfileResponse)
def change_password(body: ChangePasswordRequest, request: Request, response: Response, db: Session = Depends(get_db),
                    current_user: CreatorProfile = Depends(get_current_user)):
    """
    Needs the current password. Sets the new one (at least 12 characters), ends every other session (token_version + 1) and gives this
    browser a fresh cookie. Wrong current passwords count towards the same lockout as failed logins.
    """
    email, ip = current_user.email.strip().lower(), client_ip(request)
    wait = login_limits.locked_for_seconds(db, email, ip)
    if wait:
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again later.", headers={"Retry-After": str(wait)})
    if not current_user.password_hash or not verify_password(body.current_password, current_user.password_hash):
        login_limits.record_failure(db, email, ip)
        raise HTTPException(status_code=400, detail="The current password is not correct")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail="Choose a password different from the current one")
    try:
        new_hash = hash_password(body.new_password)
    except ValueError as exc:   # longer than bcrypt's 72 bytes
        raise HTTPException(status_code=422, detail=str(exc))
    current_user.password_hash = new_hash
    current_user.must_change_password = False
    version = _bump_token_version(db, current_user)
    db.commit()
    login_limits.clear_failures(db, email)
    _set_login_cookie(response, _make_jwt(current_user.id, current_user.email, version))
    return profile_to_response(current_user)
