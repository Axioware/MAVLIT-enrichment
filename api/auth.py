import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.orm import Session
from config import IS_PRODUCTION, JWT_SECRET
from pipeline.db import CreatorProfile, get_db
from pipeline.helpers.passwords import verify_password
from api.schemas import CreatorProfileResponse, profile_to_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_ALGORITHM  = "HS256"
_TOKEN_DAYS = 7

# SameSite=None is required for a cross-origin frontend's fetch() calls
# (credentials: 'include') to carry this cookie back to the API — but
# browsers reject SameSite=None without Secure, so this only takes effect
# in production (HTTPS). In dev (IS_PRODUCTION=false) this stays "lax" so
# the same-origin frontend/*.html pages keep working over plain
# http://localhost.
_CROSS_SITE_SAMESITE = "none" if IS_PRODUCTION else "lax"


class LoginRequest(BaseModel):
    email: str
    password: str


#  JWT helpers

def _make_jwt(user_id: int, email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=_TOKEN_DAYS)
    return jwt.encode(
        {"sub": str(user_id), "email": email, "exp": expire},
        JWT_SECRET,
        algorithm=_ALGORITHM,
    )


def _decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


#  Auth dependency

def get_current_user(request: Request, db: Session = Depends(get_db)) -> CreatorProfile:
    """FastAPI dependency — returns the logged-in CreatorProfile or raises 401."""
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = _decode_jwt(token)
    user = db.query(CreatorProfile).filter(CreatorProfile.id == int(payload["sub"])).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> CreatorProfile | None:
    """Like get_current_user, but returns None rather than raising when unauthenticated."""
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = _decode_jwt(token)
    except HTTPException:
        return None
    user = db.query(CreatorProfile).filter(CreatorProfile.id == int(payload["sub"])).first()
    if not user or not user.is_active:
        return None
    return user


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
        content_description = str(profile.get("content_description") or "").strip()
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
        content_description = (profile.content_description or "").strip()
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

    description_text = content_description or instagram_description or youtube_description
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

@router.post("/login", response_model=CreatorProfileResponse)
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)):
    email = body.email.strip().lower()

    user = db.query(CreatorProfile).filter(CreatorProfile.email == email).first()
    # Generic error either way — don't reveal whether the email exists.
    if not user or not user.password_hash or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account has been deactivated")

    token = _make_jwt(user.id, user.email)
    response.set_cookie(
        "access_token", token,
        httponly=True,
        max_age=60 * 60 * 24 * _TOKEN_DAYS,
        samesite=_CROSS_SITE_SAMESITE,
        secure=IS_PRODUCTION,
    )
    return profile_to_response(user)


@router.get("/me")
def me(current_user: CreatorProfile = Depends(get_current_user)):
    """Return the logged-in user's profile."""
    return profile_to_response(current_user)


@router.post("/logout")
def logout(response: Response):
    """Clear the JWT cookie."""
    # Must match the samesite/secure attributes it was set with, or some
    # browsers treat this as a different cookie and leave the real one intact.
    response.delete_cookie("access_token", samesite=_CROSS_SITE_SAMESITE, secure=IS_PRODUCTION)
    return {"message": "Logged out"}
