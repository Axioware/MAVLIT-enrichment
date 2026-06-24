"""
pipeline/helpers/social.py

Shared social-media URL and handle utilities used across enrichment steps.
"""

from urllib.parse import urlparse, urlunparse


def normalize_handle(raw: str) -> str:
    """
    Extract a bare username from any of these formats:
      '@nike'                           → 'nike'
      'nike'                            → 'nike'
      'https://www.instagram.com/nike/' → 'nike'
      'https://twitter.com/@nike'       → 'nike'
    """
    raw = raw.strip()
    if "/" in raw:
        return raw.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
    return raw.lstrip("@")


def normalize_social_url(href: str) -> str:
    """
    Normalise a social media profile URL:
      - enforce https
      - strip query string and fragment
      - remove trailing slash
    Returns href unchanged on any parse error.
    """
    try:
        p = urlparse(href)
        path = p.path.rstrip("/")
        return urlunparse(("https", p.netloc, path, "", "", ""))
    except Exception:
        return href
