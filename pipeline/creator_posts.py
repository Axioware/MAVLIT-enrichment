"""
pipeline/creator_posts.py

"Your recent Instagram posts" on the creator-profile page: the posts saved for a MAVLIT user in
creator_instagram_posts (scraped when they generate their description, or added by them here).
The user can list them, switch a post's "paid partnership" toggle, delete posts (never below
MIN_POSTS) and add a post by its URL.

Adding a post uses the same logic as the pitch (pipeline/pitch_posts.py): a post already saved for the
user is just returned, a post saved for another MAVLIT user is copied, and only a post MAVLIT has not
read before is scraped with Apify (one run for that single post).
"""

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from pipeline.db import CreatorInstagramPost, CreatorProfile
from pipeline.helpers.social import normalize_handle
from pipeline.pitch_posts import _clean_url, _scrape_posts, post_code

logger = logging.getLogger(__name__)

MIN_POSTS = 10     # a user always keeps at least this many posts: nothing can be deleted at or below it
MAX_POSTS = 50     # most posts a user can have saved (each new post may cost an Apify run)


def posts_query(db: Session, creator_id: int):
    return (
        db.query(CreatorInstagramPost)
        .filter(CreatorInstagramPost.creator_profile_id == creator_id, CreatorInstagramPost.post_id.isnot(None))
    )


def username_changed(old_handle: str | None, new_handle: str | None) -> bool:
    """True when the creator's Instagram username was changed (both given and different, ignoring case, @ and URL form)."""
    if not (old_handle or "").strip() or not (new_handle or "").strip():
        return False
    return normalize_handle(old_handle).lower() != normalize_handle(new_handle).lower()


def clear_posts(db: Session, creator_id: int) -> int:
    """Delete ALL the user's saved posts (their toggles included). Returns how many were deleted."""
    deleted = db.query(CreatorInstagramPost).filter(CreatorInstagramPost.creator_profile_id == creator_id).delete(synchronize_session=False)
    db.commit()
    if deleted:
        logger.info("Cleared %d saved post(s) of creator_id=%s (Instagram username changed)", deleted, creator_id)
    return deleted


def clear_posts_if_username_changed(db: Session, creator_id: int, old_handle: str | None, new_handle: str | None) -> int:
    """
    Profile save: when the username changed and none of the user's saved posts belong to the NEW username, the
    old username's posts are emptied (the table is filled again when the description is generated). If the new
    username's posts are already there, generating did that work, so nothing is touched.
    """
    if not username_changed(old_handle, new_handle):
        return 0
    already = (
        posts_query(db, creator_id)
        .filter(func.lower(CreatorInstagramPost.instagram_handle) == normalize_handle(new_handle).lower())
        .count()
    )
    return 0 if already else clear_posts(db, creator_id)


def list_posts(db: Session, creator_id: int, search: str | None = None, partnership_only: bool = False) -> list[CreatorInstagramPost]:
    """
    The user's saved posts, newest first. With `search`, only the posts whose WHOLE caption contains that text (any
    case); with `partnership_only`, only the posts the user marked as paid partnerships.
    """
    query = posts_query(db, creator_id)
    if partnership_only:
        query = query.filter(CreatorInstagramPost.partnership_post.is_(True))
    text_ = (search or "").strip()
    if text_:
        escaped = text_.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")   # % and _ are searched as plain characters
        query = query.filter(CreatorInstagramPost.caption.ilike(f"%{escaped}%", escape="\\"))
    return query.order_by(CreatorInstagramPost.timestamp.desc().nullslast(), CreatorInstagramPost.fetched_at.desc()).all()


def get_post(db: Session, creator_id: int, row_id: int) -> CreatorInstagramPost | None:
    return posts_query(db, creator_id).filter(CreatorInstagramPost.id == row_id).first()


def set_partnership(db: Session, row: CreatorInstagramPost, value: bool) -> CreatorInstagramPost:
    row.partnership_post = bool(value)
    db.commit()
    db.refresh(row)
    return row


def delete_post(db: Session, creator_id: int, row: CreatorInstagramPost) -> None:
    """Delete one post. Raises ValueError when that would leave the user with fewer than MIN_POSTS."""
    total = posts_query(db, creator_id).count()
    if total - 1 < MIN_POSTS:
        raise ValueError(f"At least {MIN_POSTS} posts have to stay on your profile, so this post cannot be deleted.")
    db.delete(row)
    db.commit()


def _find_by_code(db: Session, creator_id: int | None, code: str) -> CreatorInstagramPost | None:
    query = db.query(CreatorInstagramPost).filter(
        CreatorInstagramPost.post_id.isnot(None), func.strpos(CreatorInstagramPost.post_url, "/" + code) > 0
    )
    if creator_id is not None:
        query = query.filter(CreatorInstagramPost.creator_profile_id == creator_id)
    else:
        query = query.order_by(CreatorInstagramPost.fetched_at.desc())
    return query.first()


def add_post(db: Session, creator: CreatorProfile, post_url: str) -> tuple[CreatorInstagramPost, str]:
    """
    Add a post by its Instagram URL. Returns (row, how): how is "already_added", "copied" (saved for another
    user, no Apify run) or "scraped". Raises ValueError for a bad URL / too many posts, LookupError when the
    post cannot be read (private, deleted or Instagram could not be reached).
    """
    code = post_code(post_url or "")
    if not code:
        raise ValueError("Paste the link of an Instagram post or reel, like https://www.instagram.com/p/XXXX/")
    existing = _find_by_code(db, creator.id, code)
    if existing is not None:
        return existing, "already_added"
    if posts_query(db, creator.id).count() >= MAX_POSTS:
        raise ValueError(f"You can keep up to {MAX_POSTS} posts. Delete one to add another.")

    other = _find_by_code(db, None, code)
    if other is not None:   # saved for another MAVLIT user: copy it, no Apify
        copy = CreatorInstagramPost(
            creator_profile_id=creator.id, instagram_handle=other.instagram_handle, post_id=other.post_id,
            post_url=other.post_url, post_type=other.post_type, timestamp=other.timestamp, caption=other.caption,
            hashtags=other.hashtags, mentions=other.mentions, tagged_users=other.tagged_users,
            paid_partnership=other.paid_partnership, likes_count=other.likes_count, comments_count=other.comments_count,
            video_view_count=other.video_view_count, video_play_count=other.video_play_count,
            followers_count=other.followers_count, biography=other.biography, partnership_post=False,
        )
        db.add(copy)
        db.commit()
        db.refresh(copy)
        return copy, "copied"

    found = _scrape_posts(db, creator, {code: _clean_url(post_url)})   # one Apify run; saves the post for the user
    row = _find_by_code(db, creator.id, code) if code in found else None
    if row is None:
        raise LookupError("MAVLIT could not read this post. It may be private or deleted, or Instagram is not answering. Please check the link and try again.")
    return row, "scraped"
