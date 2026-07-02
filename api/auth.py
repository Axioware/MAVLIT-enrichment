"""
api/auth.py

Google OAuth 2.0 sign-in / sign-up.

Routes:
  GET  /auth/google           → redirect user to Google consent screen
  GET  /auth/google/callback  → exchange code, create/find user, set JWT cookie
  GET  /auth/me               → return current user (requires cookie)
  POST /auth/logout           → clear JWT cookie
"""

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, JWT_SECRET, OAUTH_REDIRECT_URI
from pipeline.db import User, get_db

router = APIRouter(prefix="/auth", tags=["auth"])

_GOOGLE_AUTH_URL  = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO  = "https://www.googleapis.com/oauth2/v2/userinfo"
_ALGORITHM        = "HS256"
_TOKEN_DAYS       = 7


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

def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """FastAPI dependency — returns the logged-in User or raises 401."""
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = _decode_jwt(token)
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


#  Routes 

@router.get("/google")
def login_google():
    """Step 1 — redirect the browser to Google's OAuth consent screen."""
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
        httponly=True, max_age=600, samesite="lax", secure=False,
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
        return response

    # Verify CSRF state
    cookie_state = request.cookies.get("oauth_state")
    if not cookie_state or cookie_state != state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    # Exchange authorization code for access token
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(_GOOGLE_TOKEN_URL, data={
            "code":          code,
            "client_id":     GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri":  OAUTH_REDIRECT_URI,
            "grant_type":    "authorization_code",
        })
        token_data = token_resp.json()
        if "error" in token_data:
            raise HTTPException(
                status_code=400,
                detail=f"Google error: {token_data.get('error_description', token_data['error'])}",
            )

        # Fetch Google profile using the access token
        info_resp = await client.get(
            _GOOGLE_USERINFO,
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        g = info_resp.json()

    # Find or create user in DB
    user = db.query(User).filter(User.google_id == g["id"]).first()
    if not user:
        user = db.query(User).filter(User.email == g["email"]).first()
        if user:
            # Existing email — link Google account to it
            user.google_id  = g["id"]
            user.avatar_url = g.get("picture")
        else:
            # Brand-new user — create row
            user = User(
                email=g["email"],
                name=g.get("name"),
                google_id=g["id"],
                avatar_url=g.get("picture"),
            )
            db.add(user)
        db.commit()
        db.refresh(user)

    # Issue JWT in a secure httpOnly cookie and redirect to dashboard
    token = _make_jwt(user.id, user.email)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        "access_token", token,
        httponly=True,
        max_age=60 * 60 * 24 * _TOKEN_DAYS,
        samesite="lax",
        secure=False,  # set True in production (requires HTTPS)
    )
    response.delete_cookie("oauth_state")
    return response


@router.get("/me")
def me(current_user: User = Depends(get_current_user)):
    """Return the logged-in user's profile."""
    return {
        "id":         current_user.id,
        "email":      current_user.email,
        "name":       current_user.name,
        "avatar_url": current_user.avatar_url,
    }


@router.post("/logout")
def logout(response: Response):
    """Clear the JWT cookie."""
    response.delete_cookie("access_token")
    return {"message": "Logged out"}
