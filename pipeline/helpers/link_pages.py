"""
Link-shortener / link-in-bio pages (bit.ly, short.io custom domains like
qob.uz, linktr.ee, beacons.ai, lnk.to, ...) are not a brand's own website.

Shared by brand_instagram_profile.py (never save one as brands_raw.website)
and geo_reach.py (when one is already stored, crawl the real brand site
behind it instead).

  - is_link_page_url(url)        -> known shortener / link-in-bio domain
  - is_link_page(url, html)      -> domain OR page fingerprint (custom short
                                    domains like qob.uz only show up here)
  - final_url(url)               -> where a URL redirects to (cheap GET)
  - outbound_links(html, url)    -> external links on a link page
  - pick_brand_link(links, name) -> the link that is the brand's own site
  - search_brand_site(name)      -> SearXNG fallback when the link page has
                                    no usable links (e.g. qob.uz's short.io
                                    landing page has none at all)
  - resolve_link_page(...)       -> all of the above in order
"""

import json
import logging
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from config import SEARXNG_URL

logger = logging.getLogger(__name__)

_TIMEOUT = 12
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

# URL shorteners and link-in-bio / smart-link services. Matched on the domain
# or any subdomain of it.
LINK_PAGE_DOMAINS = frozenset([
    # shorteners
    "bit.ly", "bitly.com", "short.io", "short.gy", "tinyurl.com", "t.co", "ow.ly",
    "buff.ly", "rebrand.ly", "rebrandly.com", "rb.gy", "cutt.ly", "is.gd", "goo.gl", "tiny.cc",
    "shorturl.at", "s.id", "t.ly", "bl.ink", "shor.by", "dub.sh", "dub.co",
    # link-in-bio
    "linktr.ee", "linktree.com", "lnk.bio", "linkin.bio", "beacons.ai", "bio.link",
    "bio.site", "solo.to", "tap.bio", "taplink.cc", "hoo.be", "campsite.bio",
    "allmylinks.com", "msha.ke", "milkshake.app", "linkfly.to", "many.link",
    "manylink.co", "heylink.me", "linkpop.com", "flow.page", "snipfeed.co",
    "stan.store", "koji.to", "withkoji.com", "direct.me", "contactin.bio",
    "biolinky.co", "url.bio", "liinks.co", "link.space", "joy.link", "linkr.bio",
    "znap.link", "carrd.co",
    # music smart links
    "lnk.to", "ffm.to", "smarturl.it", "found.ee", "fanlink.to", "fanlink.tv",
    "hypeddit.com", "linkfire.com", "toneden.io", "show.co", "songwhip.com",
    "push.fm", "orcd.co",
])

# Page fingerprints for custom-domain short links / link-in-bio pages whose
# domain says nothing (qob.uz is a short.io page).
_LINK_PAGE_HTML_MARKERS = (
    "js.short.io", "shortiougc.com", "assets.production.linktr.ee", "linktr.ee/",
    "cdn.beacons.ai", "bio.link/", "lnk.bio/", "taplink.cc", "linkfire.com",
    "rebrandly", "rb.routing", "bitly.com",
)

# Never a brand's own website either (link candidates are filtered by these).
_PLATFORM_DOMAINS = frozenset([
    "instagram.com", "youtube.com", "youtu.be", "tiktok.com", "facebook.com", "fb.com",
    "x.com", "twitter.com", "linkedin.com", "pinterest.com", "threads.net",
    "spotify.com", "open.spotify.com", "music.apple.com", "apple.com", "soundcloud.com",
    "bandcamp.com", "deezer.com", "tidal.com", "amazon.com", "patreon.com",
    "reddit.com", "tumblr.com", "discord.gg", "discord.com", "twitch.tv",
    "wa.me", "whatsapp.com", "t.me", "telegram.me", "snapchat.com",
    "google.com", "fonts.googleapis.com", "fonts.gstatic.com",
])


def _domain(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    except Exception:
        return ""
    return netloc[4:] if netloc.startswith("www.") else netloc


def _matches(domain: str, bases) -> bool:
    return any(domain == b or domain.endswith("." + b) for b in bases)


def is_link_page_url(url: str) -> bool:
    return _matches(_domain(url), LINK_PAGE_DOMAINS)


def is_link_page(url: str, html: str | None = None) -> bool:
    if is_link_page_url(url):
        return True
    if not html:
        return False
    head = html[:20000].lower()
    return any(m in head for m in _LINK_PAGE_HTML_MARKERS)


def _is_candidate(url: str) -> bool:
    d = _domain(url)
    return bool(d) and not _matches(d, _PLATFORM_DOMAINS) and not _matches(d, LINK_PAGE_DOMAINS)


def final_url(url: str) -> tuple[str, str | None]:
    """(url after redirects, html) via a plain GET — (url, None) on failure."""
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=_TIMEOUT, follow_redirects=True)
        return str(resp.url), resp.text if resp.status_code < 400 else None
    except Exception as exc:
        logger.debug("link_pages: GET %s failed: %s", url, exc)
        return url, None


_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_JSON_URL_RE = re.compile(r'"url"\s*:\s*"(https?:(?:\\/|/)[^"\s]+)"')


def embedded_json_urls(html: str) -> list[str]:
    """
    Links kept in inline JSON instead of <a> tags. Linktree pages carry the
    profile's real links in __NEXT_DATA__ (props.pageProps.links) — only
    those are used there, since the same blob also holds storefront/ad URLs.
    Other pages: any "url":"https://..." value.
    """
    m = _NEXT_DATA_RE.search(html or "")
    if m:
        try:
            props = json.loads(m.group(1)).get("props", {}).get("pageProps", {})
        except ValueError:
            props = None
        if isinstance(props, dict) and "links" in props:
            urls = [l.get("url") for l in (props.get("links") or []) if isinstance(l, dict)]
            return [u for u in urls if isinstance(u, str) and u.startswith("http")]
    return [u.replace("\\/", "/") for u in _JSON_URL_RE.findall(html or "")]


def outbound_links(html: str, page_url: str) -> list[str]:
    """External, non-platform links on a link page, in page order, one per domain."""
    soup = BeautifulSoup(html, "html.parser")
    page_domain = _domain(page_url)
    hrefs = [a["href"].strip() for a in soup.find_all("a", href=True)]
    hrefs += embedded_json_urls(html)
    seen: set[str] = set()
    out: list[str] = []
    for raw in hrefs:
        href = urljoin(page_url, raw)
        if not href.startswith("http"):
            continue
        d = _domain(href)
        if not d or d == page_domain or d in seen or not _is_candidate(href):
            continue
        seen.add(d)
        out.append(href)
    return out


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _name_matches_domain(names: list[str], url: str) -> bool:
    label = _compact(_domain(url).split(".")[0])
    if len(label) < 3:
        return False
    for n in names:
        c = _compact(n)
        # Full name in the domain (qobuz -> qobuz.com, gaiaherbs ->
        # gaiaherbs.com), or a domain that is most of the name (sugarfactory
        # for "Sugar Factory at Home") — but not a short common word inside
        # it ("quest" for "Quest Nutrition" is quest.com, Quest Software).
        if len(c) >= 3 and (c in label or (len(label) >= 4 and label in c and len(label) >= 0.6 * len(c))):
            return True
    return False


def is_own_domain(names: list[str], url: str) -> bool:
    """
    True when the URL's domain carries the brand's own name — the brand IS
    the link service (Linktree on help.linktr.ee, Bitly on bitly.com), so
    its website must not be treated as a link page and "resolved" away.
    """
    labels = [l for l in _domain(url).split(".")[:-1] if len(l) >= 4]
    for n in names:
        c = _compact(n)
        if len(c) >= 4 and any(c in _compact(l) or _compact(l) in c for l in labels):
            return True
    return False


def pick_brand_link(links: list[str], names: list[str]) -> str | None:
    """
    The link whose domain carries the brand name. Never "the only link" — on
    a custom short domain that is usually the link service's own homepage
    (onaquest.co's only link is rebrandly.com).
    """
    for link in links:
        if _is_candidate(link) and _name_matches_domain(names, link):
            return link
    return None


def search_brand_site(names: list[str]) -> str | None:
    """SearXNG "<name> official website": first result whose domain carries the brand name."""
    name = next((n for n in names if n and n.strip()), "")
    if not name or not SEARXNG_URL:
        return None
    try:
        resp = httpx.get(
            f"{SEARXNG_URL.rstrip('/')}/search",
            params={"q": f"{name} official website", "format": "json"},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except Exception as exc:
        logger.warning("link_pages: SearXNG search failed for %r: %s", name, exc)
        return None
    for r in results[:20]:
        url = (r.get("url") or "").strip()
        if url.startswith("http") and _is_candidate(url) and _name_matches_domain(names, url):
            return url
    return None


def root_url(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}/" if p.scheme and p.netloc else url


def resolve_link_page(url: str, names: list[str], html: str | None = None, search: bool = True) -> str | None:
    """
    Real brand site (root URL) behind a shortener / link-in-bio URL, or None.
    1. redirects (bit.ly -> brand.com)
    2. the link page's own outbound links (rendered through the fallback
       fetcher when the plain HTML has none — linktree builds them in JS)
    3. SearXNG search for the brand's own domain (search=True)
    """
    landed, landed_html = final_url(url)
    page_html = landed_html or html
    if (_domain(landed) != _domain(url) and _is_candidate(landed)
            and not is_link_page(landed, landed_html)):
        return root_url(landed)

    links = outbound_links(page_html, landed) if page_html else []
    if not links:
        from pipeline.helpers.http import fetch_page_via_jina
        rendered = fetch_page_via_jina(landed)
        if rendered:
            links = outbound_links(rendered.html, rendered.url or landed)
    link = pick_brand_link(links, names)
    if link:
        return root_url(link)

    if search:
        found = search_brand_site(names)
        if found:
            return root_url(found)
    return None
