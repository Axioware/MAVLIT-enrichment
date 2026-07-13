"""
api/auth.py

Google OAuth 2.0 sign-in / sign-up.

Routes:
  GET  /auth/google           → redirect user to Google consent screen
  GET  /auth/google/callback  → exchange code, create/find user, set JWT cookie
  GET  /auth/me               → return current user (requires cookie)
  POST /auth/logout           → clear JWT cookie
"""

import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, IS_PRODUCTION, JWT_SECRET, OAUTH_REDIRECT_URI
from pipeline.db import CreatorProfile, get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO  = "https://www.googleapis.com/oauth2/v2/userinfo"
_ALGORITHM        = "HS256"
_TOKEN_DAYS       = 7
_HTTP_TIMEOUT     = 10.0


def _safe_return_to(path: str | None) -> str:
    """
    Only allow same-origin relative paths as a post-login redirect target.
    Rejects protocol-relative ("//evil.com"), absolute ("https://evil.com"),
    and backslash-based ("/\\evil.com") open-redirect tricks.
    """
    if not path or not path.startswith("/") or path.startswith("//") or path.startswith("/\\") or ":" in path:
        return "/"
    return path


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


#  Routes

@router.get("/google")
def login_google(return_to: str | None = None):
    """Step 1 — redirect the browser to Google's OAuth consent screen.

    Pass ?return_to=/some/path to send the user back to that page after
    login instead of the default dashboard — e.g. so a form filled out
    before signing in can be resumed and saved once the session exists.
    """
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="GOOGLE_CLIENT_ID not configured")

    state = secrets.token_urlsafe(32)
    params = {
        "client_id":     GOOGLE_CLIENT_ID,
        "redirect_uri":  OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope":         "openid email profile",
        "access_type":   "offline",
        "state":         state,
        "prompt":        "select_account",
    }
    response = RedirectResponse(url=f"{_GOOGLE_AUTH_URL}?{urlencode(params)}")
    # Store state in a short-lived httpOnly cookie for CSRF verification
    response.set_cookie(
        "oauth_state", state,
        httponly=True, max_age=600, samesite="lax", secure=IS_PRODUCTION,
    )
    response.set_cookie(
        "oauth_return_to", _safe_return_to(return_to),
        httponly=True, max_age=600, samesite="lax", secure=IS_PRODUCTION,
    )
    return response


@router.get("/google/callback")
async def google_callback(
    request: Request,
    db: Session = Depends(get_db),
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Step 2 — Google redirects here with ?code=&state= (or ?error= if user cancelled)."""
    # User cancelled the Google consent screen
    if error or not code:
        response = RedirectResponse(url="/signin", status_code=302)
        response.delete_cookie("oauth_state")
        response.delete_cookie("oauth_return_to")
        return response

    # Verify CSRF state (constant-time compare)
    cookie_state = request.cookies.get("oauth_state")
    if not cookie_state or not state or not hmac.compare_digest(cookie_state, state):
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    # Exchange authorization code for access token
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            token_resp = await client.post(_GOOGLE_TOKEN_URL, data={
                "code":          code,
                "client_id":     GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri":  OAUTH_REDIRECT_URI,
                "grant_type":    "authorization_code",
            })
            try:
                token_data = token_resp.json()
            except ValueError:
                logger.error("Google token endpoint returned non-JSON response: %s", token_resp.text[:300])
                raise HTTPException(status_code=502, detail="Google sign-in failed — please try again")

            if token_resp.status_code != 200 or "error" in token_data:
                logger.warning(
                    "Google token exchange failed: HTTP %s — %s",
                    token_resp.status_code, token_data.get("error_description", token_data.get("error")),
                )
                raise HTTPException(status_code=400, detail="Google sign-in failed — please try again")

            access_token = token_data.get("access_token")
            if not access_token:
                logger.error("Google token response missing access_token")
                raise HTTPException(status_code=502, detail="Google sign-in failed — please try again")

            # Fetch Google profile using the access token
            info_resp = await client.get(
                _GOOGLE_USERINFO,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if info_resp.status_code != 200:
                logger.error("Google userinfo request failed: HTTP %s", info_resp.status_code)
                raise HTTPException(status_code=502, detail="Failed to fetch Google profile")

            try:
                g = info_resp.json()
            except ValueError:
                logger.error("Google userinfo returned non-JSON response")
                raise HTTPException(status_code=502, detail="Failed to fetch Google profile")
    except httpx.HTTPError as exc:
        # Network-level failure (timeout, connection refused, DNS, etc.) reaching
        # Google — not a bug in our logic, just a transient outbound call failing.
        logger.error("Network error talking to Google during sign-in: %s", exc)
        raise HTTPException(status_code=502, detail="Couldn't reach Google — please try again")

    if not g.get("id") or not g.get("email"):
        logger.error("Google userinfo response missing required fields: %s", list(g.keys()))
        raise HTTPException(status_code=502, detail="Incomplete Google profile response")

    # Reject unverified emails — prevents account-takeover via email-based
    # auto-linking to an existing user that doesn't actually belong to this person.
    if not g.get("verified_email", False):
        logger.warning("Rejected Google sign-in with unverified email: %s", g.get("email"))
        raise HTTPException(status_code=403, detail="Google account email is not verified")

    # Find or create the creator's account row in DB
    user = db.query(CreatorProfile).filter(CreatorProfile.google_id == g["id"]).first()
    if not user:
        user = db.query(CreatorProfile).filter(CreatorProfile.email == g["email"]).first()
        if user:
            # Existing email — link Google account to it
            user.google_id  = g["id"]
            user.avatar_url = g.get("picture")
        else:
            # Brand-new account — create row. Creator-specific fields
            # (creator_handle, content_niche, etc.) stay NULL until the
            # user fills out the creator profile form.
            user = CreatorProfile(
                email=g["email"],
                full_name=g.get("name"),
                google_id=g["id"],
                avatar_url=g.get("picture"),
            )
            db.add(user)
        try:
            db.commit()
        except IntegrityError:
            # Concurrent request (double-click / duplicate tab) already created
            # this account — roll back and re-fetch instead of erroring out.
            db.rollback()
            user = (
                db.query(CreatorProfile)
                .filter((CreatorProfile.google_id == g["id"]) | (CreatorProfile.email == g["email"]))
                .first()
            )
            if not user:
                raise HTTPException(status_code=500, detail="Sign-in failed — please try again")
        else:
            db.refresh(user)

    if not user.is_active:
        raise HTTPException(status_code=403, detail="This account has been deactivated")

    # Issue JWT in a secure httpOnly cookie and redirect back to where the
    # user started (or the dashboard by default)
    return_to = _safe_return_to(request.cookies.get("oauth_return_to"))
    token = _make_jwt(user.id, user.email)
    response = RedirectResponse(url=return_to, status_code=302)
    response.set_cookie(
        "access_token", token,
        httponly=True,
        max_age=60 * 60 * 24 * _TOKEN_DAYS,
        samesite="lax",
        secure=IS_PRODUCTION,
    )
    response.delete_cookie("oauth_state")
    response.delete_cookie("oauth_return_to")
    return response


@router.get("/me")
def me(current_user: CreatorProfile = Depends(get_current_user)):
    """Return the logged-in user's profile."""
    return {
        "id":         current_user.id,
        "email":      current_user.email,
        "name":       current_user.full_name,
        "avatar_url": current_user.avatar_url,
    }


@router.post("/logout")
def logout(response: Response):
    """Clear the JWT cookie."""
    response.delete_cookie("access_token")
    return {"message": "Logged out"}
