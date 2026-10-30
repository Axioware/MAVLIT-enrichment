"""
api/paging.py

Numeric limits of the list endpoints and of the heavy per-request queries, each with an environment override (read once, at import; a bad
value stops the application at start):

    MAX_OFFSET               10000  the largest `offset` /matches/me, /pitches/me and /saved-brands/me accept (HTTP 422 beyond it)
    SAVED_BRANDS_PAGE_DEFAULT  100  GET /saved-brands/me: rows when `limit` is not given
    SAVED_BRANDS_PAGE_MAX      500  GET /saved-brands/me: the largest `limit` (the same as the SAVED_BRANDS_MAX cap, so one page can hold all)
    BRAND_POSTS_LIMIT          200  /matches/me/brands/{id}/posts: rows read from each of the two post tables
"""

import os


def _int(name: str, default: int, minimum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


MAX_OFFSET = _int("MAX_OFFSET", 10_000, 0)
SAVED_BRANDS_PAGE_MAX = _int("SAVED_BRANDS_PAGE_MAX", 500, 1)
SAVED_BRANDS_PAGE_DEFAULT = min(_int("SAVED_BRANDS_PAGE_DEFAULT", 100, 1), SAVED_BRANDS_PAGE_MAX)
BRAND_POSTS_LIMIT = _int("BRAND_POSTS_LIMIT", 200, 1)
