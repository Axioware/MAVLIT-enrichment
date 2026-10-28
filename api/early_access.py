import re
import time
from collections import defaultdict, deque
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from pipeline.db import EarlyAccessSignup, get_db

# Public (no login): the landing page's "Join the founding creators" form.
router = APIRouter(tags=["early-access"])

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_RATE_LIMIT, _RATE_WINDOW_SECONDS = 10, 3600   # sign-ups per IP per hour
_recent: dict[str, deque] = defaultdict(deque)


class EarlyAccessRequest(BaseModel):
    email: str = Field(max_length=254)
    handle: str = Field(min_length=1, max_length=100)
    platform: Literal["instagram", "tiktok", "youtube", "substack", "other"]
    niche: Literal["music", "health", "food", "beauty", "fitness", "other"]
    followers: Literal["under10k", "10k-50k", "50k-200k", "200k-500k", "500k+"]
    website: str = ""   # honeypot: hidden on the page, so only bots fill it in

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL.match(v):
            raise ValueError("A valid email is required.")
        return v

    @field_validator("handle")
    @classmethod
    def _handle(cls, v: str) -> str:
        v = v.strip().lstrip("@").strip()
        if not v:
            raise ValueError("Your creator handle is required.")
        return v


def _too_many(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")
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
    values = {"email": body.email, "handle": body.handle, "platform": body.platform, "niche": body.niche, "followers": body.followers}
    stmt = pg_insert(EarlyAccessSignup).values(**values).on_conflict_do_update(
        index_elements=[EarlyAccessSignup.email], set_={k: v for k, v in values.items() if k != "email"},
    )
    db.execute(stmt)
    db.commit()
    return {"ok": True}   # the same answer for a new and a repeat email
