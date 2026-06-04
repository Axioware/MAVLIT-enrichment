"""
pipeline/verify.py

Stage 4 of the brand enrichment pipeline — the final gate before outreach.
Reads contacts WHERE email IS NOT NULL AND email_status IS NULL,
verifies each address via Hunter, and writes back email_verified, email_status,
email_score, and verified_at.

Runs nightly at 2:45am via scheduler.py.
Never send outreach to a contact where email_verified != true.
"""

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from config import HUNTER_API_KEY, VERIFY_BATCH_SIZE
from pipeline.db import Contact

logger = logging.getLogger(__name__)


def _get_credits_remaining() -> int:
    try:
        resp = httpx.get(
            "https://api.hunter.io/v2/account",
            params={"api_key": HUNTER_API_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        calls = resp.json()["data"]["calls"]["verifications"]
        return calls["available"] - calls["used"]
    except Exception:
        logger.exception("Failed to fetch Hunter credit balance")
        return 0


def _verify_email(email: str) -> dict:
    resp = httpx.get(
        "https://api.hunter.io/v2/email-verifier",
        params={"email": email, "api_key": HUNTER_API_KEY},
        timeout=15,  # SMTP handshake can be slow
    )
    resp.raise_for_status()
    return resp.json().get("data", {})


def run_verify(db: Session, batch_size: int = VERIFY_BATCH_SIZE) -> int:
    """
    Verify one batch of unverified contact emails via Hunter.
    Returns the count of contacts that came back with status=valid.
    Skips gracefully if Hunter credits fall below 10.
    """
    if _get_credits_remaining() < 10:
        logger.warning("Hunter credits below 10 — skipping verify run")
        return 0

    contacts = (
        db.execute(
            select(Contact)
            .where(Contact.email.is_not(None))
            .where(Contact.email_status.is_(None))
            .limit(batch_size)
        )
        .scalars()
        .all()
    )
    logger.info("Verify batch: %d contacts to process", len(contacts))

    verified_count = 0
    for contact in contacts:
        try:
            data = _verify_email(contact.email)
            status = data.get("status", "unknown")
            is_valid = status == "valid"

            updates: dict = {
                "email_verified": is_valid,
                "email_status": status,
                "email_score": data.get("score"),
                "verified_at": datetime.now(timezone.utc),
            }

            # Discard guessed emails confirmed invalid — keep the row for audit trail
            if status == "invalid" and contact.email_guessed:
                updates["email"] = None

            db.execute(update(Contact).where(Contact.id == contact.id).values(**updates))
            db.commit()

            if is_valid:
                verified_count += 1
            logger.debug("Verified contact %d — email=%s status=%s", contact.id, contact.email, status)

        except Exception:
            logger.exception("Verification failed for contact %d", contact.id)
            db.rollback()

    logger.info("Verify done — %d valid out of %d processed", verified_count, len(contacts))
    return verified_count
