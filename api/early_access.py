import re
import time
from collections import defaultdict, deque
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from api.client_ip import client_ip
from pipeline.db import CreatorProfile, EarlyAccessSignup, get_db

# Public (no login): the landing page's "Join the founding creators" form.
router = APIRouter(tags=["early-access"])

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_RATE_LIMIT, _RATE_WINDOW_SECONDS = 10, 3600   # sign-ups per IP per hour
_NICHES = ("music", "health", "fitness", "beauty")   # saved in this order, without repeats, as "music, beauty"
_recent: dict[str, deque] = defaultdict(deque)


class EarlyAccessRequest(BaseModel):
    email: str = Field(max_length=254)
    handle: str = Field(min_length=1, max_length=100)
    platform: Literal["instagram", "tiktok", "youtube", "other"]
    niche: list[Literal["music", "health", "fitness", "beauty"]] = Field(min_length=1, max_length=4)   # one or more
    followers: Literal["under10k", "10k-50k", "50k-200k", "200k-500k", "500k+"]
    website: str = ""   # honeypot: hidden on the page, so only bots fill it in

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL.match(v):
            raise ValueError("A valid email is required.")
        return v

    @field_validator("niche", mode="before")
    @classmethod
    def _niche(cls, v):
        return [v] if isinstance(v, str) else v   # the page used to send one niche as a plain string

    @field_validator("handle")
    @classmethod
    def _handle(cls, v: str) -> str:
        v = v.strip().lstrip("@").strip()
        if not v:
            raise ValueError("Your creator handle is required.")
        return v


def _too_many(request: Request) -> bool:
    ip = client_ip(request)   # not X-Forwarded-For: a client can write anything into it
    now, window = time.monotonic(), _recent[ip]
    while window and now - window[0] > _RATE_WINDOW_SECONDS:
        window.popleft()
    if len(window) >= _RATE_LIMIT:
        return True
    window.append(now)
    return False


@router.post("/early-access", status_code=201)
def join_early_access(request: Request, body: EarlyAccessRequest, db: Session = Depends(get_db)):
    if body.website:   # a bot: pretend it worked
        return {"ok": True}
    if _too_many(request):
        raise HTTPException(status_code=429, detail="Too many sign-ups from this address. Please try again later.")
    # One sign-up per email: an email that already has an account, or already filled in this form, is told so.
    if db.query(CreatorProfile.id).filter(CreatorProfile.email == body.email).first() is not None:
        raise HTTPException(status_code=409, detail="You already have a MAVLIT account with this email. Sign in at app.mavlit.com.")
    already = HTTPException(status_code=409, detail="You've already filled in this form with this email. We'll email you when it's your turn.")
    values = {"email": body.email, "handle": body.handle, "platform": body.platform, "niche": ", ".join(n for n in _NICHES if n in body.niche), "followers": body.followers}
    inserted = db.execute(pg_insert(EarlyAccessSignup).values(**values).on_conflict_do_nothing(index_elements=[EarlyAccessSignup.email])
                          .returning(EarlyAccessSignup.id)).scalar()
    db.commit()
    if inserted is None:   # a row with this email exists (two submissions at once end here too): the first one stays as it was
        raise already
    return {"ok": True}
