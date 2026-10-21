from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.auth import get_current_user
from pipeline.creator_posts import MIN_POSTS, add_post, delete_post, get_post, list_posts, set_partnership
from pipeline.db import CreatorInstagramPost, CreatorProfile, get_db

router = APIRouter(prefix="/creator-profile/me/posts", tags=["creator-posts"])

_CAPTION_PREVIEW_CHARS = 90


class CreatorPostItem(BaseModel):
    id: int
    post_url: str | None = None
    caption_preview: str | None = None      # the start of the caption only
    post_type: str | None = None
    timestamp: str | None = None
    likes_count: int | None = None
    comments_count: int | None = None
    views_count: int | None = None          # Instagram's view count (reels / videos), null when there is none
    is_partnership_post: bool = False       # the creator's "paid partnership" toggle


class CreatorPostsResponse(BaseModel):
    total: int
    min_keep: int                           # posts that always stay: nothing is deleted at or below this number
    can_delete: bool
    posts: list[CreatorPostItem]


class AddPostRequest(BaseModel):
    post_url: str


class PartnershipRequest(BaseModel):
    is_partnership_post: bool


def _count(value: int | None) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None   # Instagram sends -1 when likes are hidden


def _item(row: CreatorInstagramPost) -> CreatorPostItem:
    caption = " ".join((row.caption or "").split())
    return CreatorPostItem(
        id=row.id,
        post_url=row.post_url,
        caption_preview=(caption[:_CAPTION_PREVIEW_CHARS] + ("…" if len(caption) > _CAPTION_PREVIEW_CHARS else "")) or None,
        post_type=row.post_type,
        timestamp=row.timestamp,
        likes_count=_count(row.likes_count),
        comments_count=_count(row.comments_count),
        views_count=_count(row.video_view_count),
        is_partnership_post=bool(row.partnership_post),
    )


def _response(db: Session, creator_id: int) -> CreatorPostsResponse:
    rows = list_posts(db, creator_id)
    return CreatorPostsResponse(total=len(rows), min_keep=MIN_POSTS, can_delete=len(rows) > MIN_POSTS, posts=[_item(r) for r in rows])


@router.get("", response_model=CreatorPostsResponse)
def get_my_posts(db: Session = Depends(get_db), current_user: CreatorProfile = Depends(get_current_user)):
    """The Instagram posts saved for the logged-in creator ("Your recent Instagram posts"). total = 0 for a creator whose Instagram was never scraped."""
    return _response(db, current_user.id)


@router.post("", response_model=CreatorPostItem)
def add_my_post(body: AddPostRequest, db: Session = Depends(get_db), current_user: CreatorProfile = Depends(get_current_user)):
    """
    Add a post by its Instagram URL. A post already saved for MAVLIT is reused; a new one is scraped with
    Apify (a few seconds to a minute). Adding a post that is already on the profile returns it unchanged.
    """
    try:
        row, _how = add_post(db, current_user, body.post_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _item(row)


def _own_post(db: Session, creator: CreatorProfile, post_id: int) -> CreatorInstagramPost:
    row = get_post(db, creator.id, post_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return row


@router.patch("/{post_id}", response_model=CreatorPostItem)
def update_my_post(post_id: int, body: PartnershipRequest, db: Session = Depends(get_db), current_user: CreatorProfile = Depends(get_current_user)):
    """Switch the "paid partnership" toggle of one of the creator's posts on or off."""
    return _item(set_partnership(db, _own_post(db, current_user, post_id), body.is_partnership_post))


@router.delete("/{post_id}", response_model=CreatorPostsResponse)
def delete_my_post(post_id: int, db: Session = Depends(get_db), current_user: CreatorProfile = Depends(get_current_user)):
    """Delete one post; refused (400) when fewer than 10 posts would stay. Returns the updated list."""
    row = _own_post(db, current_user, post_id)
    try:
        delete_post(db, current_user.id, row)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _response(db, current_user.id)
