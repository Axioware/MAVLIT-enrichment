"""Derive a brand's bare domain from its website URL.

The domain is not stored: brands_raw.website is the single source of truth and
the domain is computed from it wherever it is needed.
"""
from urllib.parse import urlparse


def website_domain(website: str | None) -> str:
    """'https://www.nike.com/us/' -> 'nike.com'; '' when there is no usable host."""
    if not website:
        return ""
    url = website.strip()
    if "//" not in url:
        url = "//" + url
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host
