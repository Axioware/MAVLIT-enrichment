"""
A small log of what users do that no other table records, for the admin Usage & Cost page (api/usage_dashboard.py): each row is one sentence ("Added an
Instagram post") and a kind. It is a usage record only: writing it never changes what the user's request does, and a failure to write it is logged and ignored.
"""
import logging

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from pipeline.db import UserActivity

logger = logging.getLogger(__name__)

POST_ADDED = "instagram_post_added"
POST_REMOVED = "instagram_post_removed"

_SENTENCES = {POST_ADDED: "Added an Instagram post", POST_REMOVED: "Removed an Instagram post"}


def record_activity(db: Session, creator_id: int, kind: str) -> None:
    """Note that this user did `kind` just now. Never raises; the row is committed on its own."""
    try:
        db.add(UserActivity(creator_profile_id=creator_id, kind=kind, detail=_SENTENCES[kind]))
        db.commit()
    except (SQLAlchemyError, KeyError):
        db.rollback()
        logger.warning("Could not record a user activity (usage record only)")
