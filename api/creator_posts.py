import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.auth import get_current_user
from pipeline.creator_posts import MIN_POSTS, add_post, delete_post, get_post, list_posts, posts_query, set_partnership
from pipeline.db import CreatorInstagramPost, CreatorProfile, get_db
from pipeline.pitch_posts import post_code

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
    thumbnail_url: str | None = None        # GET this (signed in) for the post's picture; nothing is stored, see get_thumbnail


class CreatorPostsResponse(BaseModel):
    total: int                              # all the creator's posts, whatever the search
    shown: int = 0                          # how many are in `posts` (fewer than total when searching)
    min_keep: int                           # posts that always stay: nothing is deleted at or below this number
    can_delete: bool
    posts: list[CreatorPostItem]


class AddPostRequest(BaseModel):
    post_url: str = Field(max_length=500)
    is_partnership_post: bool = False   # also mark the post as a paid partnership (the pitch's "Past brand partnerships" picker does)


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
        thumbnail_url=f"/creator-profile/me/posts/{row.id}/thumbnail" if post_code(row.post_url or "") else None,
    )


def _response(db: Session, creator_id: int, search: str | None = None, partnership_only: bool = False) -> CreatorPostsResponse:
    total = posts_query(db, creator_id).count()   # the delete rule always looks at ALL posts, not at the search result
    rows = list_posts(db, creator_id, search, partnership_only)
    return CreatorPostsResponse(total=total, shown=len(rows), min_keep=MIN_POSTS, can_delete=total > MIN_POSTS, posts=[_item(r) for r in rows])


@router.get("", response_model=CreatorPostsResponse)
def get_my_posts(
    q: str | None = Query(default=None, max_length=100, description="only posts whose caption contains this text"),
    partnership_only: bool = Query(default=False, description="only the posts marked as paid partnerships"),
    db: Session = Depends(get_db),
    current_user: CreatorProfile = Depends(get_current_user),
):
    """
    The Instagram posts saved for the logged-in creator ("Your recent Instagram posts"). total = 0 for a creator whose
    Instagram was never scraped. `q` searches the whole caption of each post (any case); total and can_delete still
    count all posts.
    """
    return _response(db, current_user.id, q, partnership_only)


@router.post("", response_model=CreatorPostItem)
def add_my_post(body: AddPostRequest, db: Session = Depends(get_db), current_user: CreatorProfile = Depends(get_current_user)):
    """
    Add a post by its Instagram URL. A post already saved for MAVLIT is reused; a new one is scraped with
    Apify (a few seconds to a minute). Adding a post that is already on the profile returns it unchanged.
    """
    try:
        row, _how = add_post(db, current_user, body.post_url)
        if body.is_partnership_post and not row.partnership_post:
            row = set_partnership(db, row, True)
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


# The post pictures are fetched from Instagram by the server and passed on: the browser cannot load them straight from
# Instagram (its picture server answers "Cross-Origin-Resource-Policy: same-origin", so another site's <img> is blocked).
# Nothing is stored in the database; a small in-memory cache spares Instagram repeated requests.
_THUMB_TTL = 6 * 3600
_THUMB_MAX = 300
_thumb_cache: dict[str, tuple[float, bytes, str]] = {}
_THUMB_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}


def _fetch_thumbnail(code: str) -> tuple[bytes, str] | None:
    cached = _thumb_cache.get(code)
    if cached and time.time() - cached[0] < _THUMB_TTL:
        return cached[1], cached[2]
    try:
        # instagram.com/p/<code>/media/ redirects to the post's current picture (public posts only)
        resp = httpx.get(f"https://www.instagram.com/p/{code}/media/?size=m", headers=_THUMB_HEADERS, timeout=10, follow_redirects=True)
    except httpx.HTTPError:
        return None
    kind = resp.headers.get("content-type", "")
    if resp.status_code != 200 or not kind.startswith("image/") or not resp.content:
        return None
    if len(_thumb_cache) >= _THUMB_MAX:
        _thumb_cache.pop(min(_thumb_cache, key=lambda k: _thumb_cache[k][0]))   # drop the oldest
    _thumb_cache[code] = (time.time(), resp.content, kind)
    return resp.content, kind


@router.get("/{post_id}/thumbnail")
def get_thumbnail(post_id: int, db: Session = Depends(get_db), current_user: CreatorProfile = Depends(get_current_user)):
    """The picture of one of the creator's posts (404 when the post has none: private, deleted or Instagram not answering)."""
    row = _own_post(db, current_user, post_id)
    code = post_code(row.post_url or "")
    found = _fetch_thumbnail(code) if code else None
    if found is None:
        raise HTTPException(status_code=404, detail="No picture for this post")
    content, kind = found
    return Response(content=content, media_type=kind, headers={"Cache-Control": "private, max-age=3600"})


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
