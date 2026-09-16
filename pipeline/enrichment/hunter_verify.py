"""
pipeline/enrichment/hunter_verify.py

For brand_contacts rows that already have an email (from Apollo's paid
enrich step in apollo_contacts.py), checks real deliverability via
Hunter.io's Email Verifier API — an email Apollo found can still be
outdated, a catch-all guess, or a dead mailbox, and pitching a creator to
one wastes the outreach.

API: GET https://api.hunter.io/v2/email-verifier?email=...&api_key=...
(https://hunter.io/api-documentation/v2#email-verifier) — one email per
call, no bulk/batch endpoint used here (Hunter's bulk verifier is async/
webhook-based; at the volumes this runs at — a handful of emails per
brand — plain sequential calls are simpler and still cheap).

Writes, per contact:
  hunter_verified_at — when this check last ran (NULL = never)
  hunter_status       — Hunter's own classification: "valid", "invalid",
                        "accept_all", "webmail", "disposable", or
                        "unknown". NULL if the API call itself failed.
  hunter_score        — Hunter's 0-100 deliverability confidence. NULL if
                        the API call itself failed.

"valid" is the only status that means the mailbox was actually confirmed
to accept mail; "accept_all" means the mail server accepts anything sent
to it (so this specific address is unconfirmed, not necessarily bad) and
"unknown"/a failed call both mean Hunter couldn't determine anything — none
of these three are the same as "invalid" (confirmed bad), so callers
filtering brand_contacts for outreach should treat them differently rather
than lumping everything that isn't "valid" together as unusable.

Cost-conscious by design: 0.5 credit per verification (Hunter's own
pricing) — only ever runs against contacts that already have an email
(never spends a credit speculatively), and never re-checks a contact once
hunter_verified_at is set (pass contact_id to force a re-check).
"""

import logging

import httpx
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from config import HUNTER_API_KEY
from pipeline.db import BrandContact

logger = logging.getLogger(__name__)

_VERIFY_URL = "https://api.hunter.io/v2/email-verifier"
_TIMEOUT = 20
_VALID_STATUSES = frozenset(["valid", "invalid", "accept_all", "webmail", "disposable", "unknown"])


def _verify_email(email: str) -> dict | None:
    try:
        resp = httpx.get(
            _VERIFY_URL,
            params={
                "email": email,
                "api_key": HUNTER_API_KEY,
            },
            timeout=_TIMEOUT,
        )

        if resp.status_code == 429:
            logger.warning(
                "Hunter verify: rate limited (429) for %s",
                email,
            )
            return None

        if resp.status_code == 222:
            logger.info(
                "Hunter verify: 222 (retry later) for %s",
                email,
            )
            return None

        resp.raise_for_status()

        return resp.json().get("data")

    except httpx.ReadTimeout:
        logger.warning(
            "Hunter verify: timeout after %ss for %s",
            _TIMEOUT,
            email,
        )
        return None

    except httpx.ConnectError:
        logger.warning(
            "Hunter verify: connection error for %s",
            email,
        )
        return None

    except httpx.HTTPStatusError as exc:
        logger.error(
            "Hunter verify: HTTP %s for %s: %s",
            exc.response.status_code,
            email,
            exc.response.text[:500],
        )
        return None

    except httpx.RequestError as exc:
        logger.warning(
            "Hunter verify: request error for %s: %s",
            email,
            exc,
        )
        return None


def _apply_verification(db: Session, contact: BrandContact, data: dict | None) -> None:
    if not data:
        # API call itself failed — leave hunter_verified_at unset so this
        # contact is retried on the next run, rather than recording a
        # false "checked, unknown".
        return

    status = data.get("status")
    contact.hunter_status = status if status in _VALID_STATUSES else "unknown"
    contact.hunter_score = data.get("score")
    contact.hunter_verified_at = func.now()
    db.commit()

    logger.info(
        "Hunter verify: contact_id=%s '%s' <%s> -> status=%s score=%s",
        contact.id, contact.full_name, contact.email, contact.hunter_status, contact.hunter_score,
    )


def run_hunter_verify(db: Session, limit: int = 50, contact_id: int | None = None) -> int:
    """
    Verifies deliverability for pending brand_contacts rows (email set,
    hunter_verified_at IS NULL). Pass contact_id to target one specific
    brand_contacts row directly — bypasses the already-verified filter, so
    you can re-check a contact regardless of its current state.

    Returns number of contacts processed (verified or attempted-and-failed).
    """
    if not HUNTER_API_KEY:
        logger.warning("HUNTER_API_KEY not set — skipping email verification")
        return 0

    query = db.query(BrandContact).filter(BrandContact.email.isnot(None))
    if contact_id is not None:
        query = query.filter(BrandContact.id == contact_id)
    else:
        query = query.filter(BrandContact.hunter_verified_at.is_(None))

    contacts: list[BrandContact] = query.limit(limit).all()
    if not contacts:
        logger.info("Hunter verify: no pending contacts")
        return 0

    logger.info("Hunter verify: checking %d contact(s)", len(contacts))
    processed = 0
    for contact in contacts:
        data = _verify_email(contact.email)
        _apply_verification(db, contact, data)
        processed += 1

    logger.info("Hunter verify: %d contact(s) processed", processed)
    return processed
