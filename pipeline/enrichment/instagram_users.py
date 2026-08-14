"""
pipeline/enrichment/instagram_users.py

For each instagram_post where is_users_scraped=False:

  1. Collect unique usernames from coauthor_producers, tagged_users, mentions.
     Each username gets a user_type label (priority: coauthor_producer > tagged_user > mention).

  2. For any username already present in instagram_users, skip Apify and link the
     existing user to the post's brand in brand_instagram_users.

  3. For each new content creator username:
     a. Scrape their top 5 posts via Apify (addParentData=True embeds profile data in every post).
     b. Extract profile data (bio, businessAddress, etc.) from the first post's parent fields.
     c. Classify demographics via the LLM (gender, country, language, location, age_group), and
        the creator's content niche via the LLM from bio + the 5 posts' captions/hashtags —
        niche classification only ever runs for creators, never for commenters (see step f).
        Both are classified ONCE per creator and duplicated identically across every one
        of that creator's post rows (see step d).
     d. Store ONE ROW PER POST in instagram_users (up to 5 rows for this creator), with
        the appropriate user_type — not nested into a single top_posts JSONB snapshot
        (that field is legacy-only now, still present on old rows, no longer written).
        Upserted on post_id, since one username can now own multiple rows.
     e. Collect up to 5 commenters per post from latestComments → up to 25 unique usernames.
     f. For each commenter NOT already in instagram_users:
        - Scrape 1 post via Apify (addParentData=True) to get their profile data.
        - Classify demographics via the LLM (no niche classification for commenters).
        - Store ONE row (commenters only ever get 1 post) with user_type="commenter"
          and niche=NULL. Upserted on username — commenters are the only user_type
          still unique-by-username (see InstagramUser's docstring in pipeline/db.py).
        Existing commenters are linked to the post's brand without scraping.
     g. Link each discovered commenter to the content creator in
        instagram_creator_commenters.

  4. Mark instagram_post.is_users_scraped=True.

Prompts (editable via /admin > Prompts):
  instagram_user_demographics  — used for both content creators and commenters
  instagram_creator_niche      — creators only, NULL/skipped for commenters
"""

import logging
import time

from sqlalchemy.orm import Session

from config import APIFY_TOKEN, OPENAI_KEY
from pipeline.db import BrandInstagramUser, InstagramCreatorCommenter, InstagramPost, InstagramUser, Prompt
from pipeline.helpers.apify import ApifyQuotaExceeded, run_apify_actor
from pipeline.helpers.creator_tier import bucket_creator_tier
from pipeline.helpers.db import upsert_rows
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template
from pipeline.helpers.prompts import (
    DEMOGRAPHICS_PROMPT_NAME, DEMOGRAPHICS_DEFAULT_PROMPT,
    CREATOR_NICHE_PROMPT_NAME, CREATOR_NICHE_DEFAULT_PROMPT,
)

logger = logging.getLogger(__name__)

_ACTOR_ID = "shu8hvrXbJbY3Eb9W"

#  Prompt helpers

def _get_demographics_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == DEMOGRAPHICS_PROMPT_NAME).first()
    return row.content if row else DEMOGRAPHICS_DEFAULT_PROMPT


def _get_niche_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == CREATOR_NICHE_PROMPT_NAME).first()
    return row.content if row else CREATOR_NICHE_DEFAULT_PROMPT


#  Collection helpers 

def _collect_creators(post: InstagramPost) -> dict[str, str]:
    """
    Returns {username: user_type} for all users in the post's collaboration fields.
    Priority: coauthor_producer overwrites tagged_user overwrites mention.
    Skips any username that matches the brand's own Instagram handle.
    """
    result: dict[str, str] = {}
    brand_handle = (post.instagram_handle or "").lower().lstrip("@")

    def _is_brand(username: str) -> bool:
        return username.lower() == brand_handle

    # lowest priority first so higher priority overwrites
    for m in post.mentions or []:
        if isinstance(m, str) and m:
            u = m.lstrip("@")
            if u and not _is_brand(u):
                result[u] = "mention"

    for entry in post.tagged_users or []:
        u = entry if isinstance(entry, str) else entry.get("username") if isinstance(entry, dict) else None
        if u:
            u = u.lstrip("@")
        if u and not _is_brand(u):
            result[u] = "tagged_user"

    for entry in post.coauthor_producers or []:
        u = entry if isinstance(entry, str) else entry.get("username") if isinstance(entry, dict) else None
        if u:
            u = u.lstrip("@")
        if u and not _is_brand(u):
            result[u] = "coauthor_producer"

    return result


def _post_url(post_item: dict) -> str | None:
    """Build an Instagram post URL from a post item using shortCode (most reliable)."""
    short = post_item.get("shortCode")
    if short:
        return f"https://www.instagram.com/p/{short}/"
    return post_item.get("url") or None


def _scrape_post_comments(post_url: str, n: int = 5) -> list[str]:
    """Fetch up to n commenter usernames for a single post URL via Apify comments call."""
    logger.info("Instagram users: fetching comments via Apify for %s", post_url)
    items = run_apify_actor(
        _ACTOR_ID,
        {
            "directUrls":   [post_url],
            "resultsType":  "comments",
            "resultsLimit": n,
        },
        label=f"IGUsers comments {post_url}",
    )
    logger.info("Instagram users: comments Apify returned %d item(s) for %s", len(items or []), post_url)
    return [
        c.get("ownerUsername", "").strip()
        for c in (items or [])
        if (c.get("ownerUsername") or "").strip()
    ]


def _collect_commenters(posts: list[dict], n_per_post: int = 5) -> list[str]:
    """
    Collect up to n_per_post unique commenter usernames across all posts.
    Uses latestComments if present; falls back to a separate Apify call per post if empty.
    """
    seen: set[str] = set()
    result: list[str] = []

    for post_item in posts:
        comments = post_item.get("latestComments") or []

        if comments:
            by_likes = sorted(comments, key=lambda c: c.get("likesCount") or 0, reverse=True)
            usernames = [
                (c.get("ownerUsername") or "").strip()
                for c in by_likes
                if (c.get("ownerUsername") or "").strip()
            ]
        else:
            url = _post_url(post_item)
            if not url:
                continue
            usernames = _scrape_post_comments(url, n=n_per_post)
            time.sleep(0.5)

        count = 0
        for uname in usernames:
            if uname and uname not in seen:
                seen.add(uname)
                result.append(uname)
                count += 1
                if count >= n_per_post:
                    break

    return result


def _collect_commenter_records(posts: list[dict], n_per_post: int = 5) -> list[dict]:
    """
    Collect commenter records with source post/comment context.
    Keeps one row per username per source post.
    """
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []

    for post_item in posts:
        post_url = _post_url(post_item) or ""
        comments = post_item.get("latestComments") or []

        if comments:
            by_likes = sorted(comments, key=lambda c: c.get("likesCount") or 0, reverse=True)
            records = [
                {
                    "username": (c.get("ownerUsername") or "").strip(),
                    "source_post_url": post_url,
                    "comment_text": c.get("text"),
                    "comment_likes": c.get("likesCount"),
                }
                for c in by_likes
                if (c.get("ownerUsername") or "").strip()
            ]
        else:
            if not post_url:
                continue
            records = [
                {
                    "username": username,
                    "source_post_url": post_url,
                    "comment_text": None,
                    "comment_likes": None,
                }
                for username in _scrape_post_comments(post_url, n=n_per_post)
            ]
            time.sleep(0.5)

        count = 0
        for record in records:
            username = record["username"]
            key = (username, post_url)
            if username and key not in seen:
                seen.add(key)
                result.append(record)
                count += 1
                if count >= n_per_post:
                    break

    return result


#  Apify helpers 

def _scrape_posts(username: str, n: int = 5) -> list[dict] | None:
    """
    Scrape n posts for a profile. addParentData=True embeds profile fields
    (fullName, biography, externalUrl, followersCount, businessAddress, etc.)
    into every post item so one call gets both posts AND profile data.

    require_success=True so a failed run (actor limit reached, network
    error, actor crash, etc.) returns None — distinguishable from a
    genuinely empty [] result (e.g. a private/deleted account), which
    callers need in order to avoid marking a row fully processed off the
    back of a call that never actually ran.
    """
    items = run_apify_actor(
        _ACTOR_ID,
        {
            "addParentData": True,
            "directUrls":    [f"https://www.instagram.com/{username}/"],
            "resultsType":   "posts",
            "resultsLimit":  n,
        },
        label=f"IGUsers @{username} (n={n})",
        require_success=True,
    )
    return None if items is None else items[:n]


def _profile_from_posts(posts: list[dict]) -> dict:
    """
    Extract profile fields embedded by addParentData from the first post item.
    The actor nests the whole profile snapshot under item["metaData"] rather
    than flattening it onto the post item's top level.
    """
    if not posts:
        return {}
    p = posts[0]
    md = p.get("metaData") or {}
    return {
        "fullName":          md.get("fullName") or p.get("ownerFullName"),
        "biography":         md.get("biography"),
        "externalUrl":       md.get("externalUrl"),
        "businessAddress":   md.get("businessAddress"),
        "followersCount":    md.get("followersCount"),
        "followsCount":      md.get("followsCount"),
        "postsCount":        md.get("postsCount"),
        # md.get("verified") or md.get("isVerified") would be wrong here — if
        # "verified" is explicitly False (the normal case for almost every
        # account), `or` treats it as falsy and falls through to
        # isVerified, which this actor never sends, silently turning a
        # confirmed False into an unknown None.
        "verified":          md.get("verified") if md.get("verified") is not None else md.get("isVerified"),
        "isBusinessAccount": md.get("isBusinessAccount"),
    }


#  LLM helper 

_UNKNOWN_DEMO = {
    "gender": "unknown", "country": "unknown",
    "language": "unknown", "location": "unknown", "age_group": "unknown",
}


def _format_business_address(addr) -> str:
    """businessAddress comes back as a dict (street_address/city_name/zip_code) for business accounts."""
    if not addr:
        return ""
    if isinstance(addr, str):
        return addr
    if isinstance(addr, dict):
        parts = [addr.get("street_address"), addr.get("city_name"), addr.get("zip_code")]
        return ", ".join(p for p in parts if p)
    return ""


def _classify_demographics(db: Session, username: str, profile: dict) -> dict:
    """Classify demographics for a user profile via the LLM."""
    if not OPENAI_KEY:
        return _UNKNOWN_DEMO

    prompt = fill_template(
        _get_demographics_prompt(db),
        username=username,
        full_name=profile.get("fullName") or "",
        bio=profile.get("biography") or "",
        external_url=profile.get("externalUrl") or "",
        business_address=_format_business_address(profile.get("businessAddress")),
    )
    result = call_gpt_json(prompt, context=f"demographics @{username}")
    if not isinstance(result, dict):
        return _UNKNOWN_DEMO
    return {
        "gender":    result.get("gender", "unknown"),
        "country":   result.get("country", "unknown"),
        "language":  result.get("language", "unknown"),
        "location":  result.get("location", "unknown"),
        "age_group": result.get("age_group", "unknown"),
    }


def _classify_niche(db: Session, username: str, profile: dict, raw_posts: list[dict]) -> str:
    """
    Classify a content creator's niche via the LLM, from their bio plus the
    captions/hashtags of the (up to 5) posts just scraped. Creators only —
    never called for commenters (see _build_post_row's niche gating).
    """
    if not OPENAI_KEY:
        return "unknown"

    captions = " | ".join((p.get("caption") or "")[:300] for p in raw_posts if p.get("caption"))
    hashtags = [tag for p in raw_posts for tag in (p.get("hashtags") or [])]

    prompt = fill_template(
        _get_niche_prompt(db),
        bio=profile.get("biography") or "",
        captions=captions or "none",
        hashtags=", ".join(hashtags) if hashtags else "none",
    )
    result = call_gpt_json(prompt, context=f"creator niche @{username}")
    if not isinstance(result, dict):
        return "unknown"
    niche = result.get("niche")
    return niche.strip() if isinstance(niche, str) and niche.strip() else "unknown"


#  Row builder

def _build_post_row(
    username: str,
    user_type: str,
    profile: dict,
    demo: dict,
    item: dict,
    niche: str | None = None,
    is_content_creator_re: bool = False,
) -> dict:
    """
    One row per post. For creators (coauthor_producer/tagged_user/mention)
    this is called once per scraped post (up to 5), with demographics/niche
    classified once and duplicated identically across every one of that
    creator's rows. For commenters it's called once (they only ever scrape
    1 post) — niche stays NULL for them, matching the prior convention.
    """
    return {
        "username":            username,
        "profile_url":         f"https://www.instagram.com/{username}/",
        "user_type":           user_type,
        "full_name":           profile.get("fullName"),
        "bio":                 profile.get("biography"),
        "external_url":        profile.get("externalUrl"),
        "followers_count":     profile.get("followersCount"),
        "tier_fit":            bucket_creator_tier(profile.get("followersCount")),
        "follows_count":       profile.get("followsCount"),
        "posts_count":         profile.get("postsCount"),
        "is_verified":         profile.get("verified"),
        "is_business_account": profile.get("isBusinessAccount"),
        "gender":              demo["gender"],
        "country":             demo["country"],
        "language":            demo["language"],
        "location":            demo["location"],
        "age_group":           demo["age_group"],
        # LLM-classified content niche — creators only, NULL for commenters.
        "niche":               niche if user_type != "commenter" else None,
        "post_id":             str(item.get("id") or ""),
        "post_url":            item.get("url") or item.get("displayUrl") or "",
        "caption":             item.get("caption"),
        "likes_count":         item.get("likesCount"),
        "comments_count":      item.get("commentsCount"),
        "post_timestamp":      item.get("timestamp"),
        "top_comments":        _top_comments_str(item),
        "is_content_creator_re": is_content_creator_re,
        # No longer written — all its fields now live in flat columns above.
        "raw_profile":         None,
    }


def _top_comments_str(item: dict) -> str | None:
    """Comma-separated comment texts from this post's latestComments."""
    comments = item.get("latestComments") or []
    texts = [c.get("text", "").strip() for c in comments if isinstance(c, dict) and c.get("text", "").strip()]
    return ", ".join(texts) if texts else None


#  Brand link helper 

def _link_to_brand(db: Session, brand_raw_id: int, username: str) -> None:
    """Insert a brand_instagram_users row linking brand → user (ignore if exists)."""
    user = db.query(InstagramUser.id).filter(InstagramUser.username == username).first()
    if user:
        upsert_rows(
            db, BrandInstagramUser,
            [{"brand_raw_id": brand_raw_id, "instagram_user_id": user.id}],
            ["brand_raw_id", "instagram_user_id"],
        )


def _link_commenter_to_creator(
    db: Session,
    brand_raw_id: int,
    creator_username: str,
    commenter_record: dict,
) -> None:
    """Insert a creator-commenter link with source context (ignore if exists)."""
    commenter_username = commenter_record.get("username")
    if not commenter_username:
        return

    creator = db.query(InstagramUser.id).filter(InstagramUser.username == creator_username).first()
    commenter = db.query(InstagramUser.id).filter(InstagramUser.username == commenter_username).first()
    if not creator or not commenter:
        return

    upsert_rows(
        db, InstagramCreatorCommenter,
        [{
            "creator_user_id": creator.id,
            "commenter_user_id": commenter.id,
            "brand_raw_id": brand_raw_id,
            "source_post_url": commenter_record.get("source_post_url") or "",
            "comment_text": commenter_record.get("comment_text"),
            "comment_likes": commenter_record.get("comment_likes"),
        }],
        ["creator_user_id", "commenter_user_id", "brand_raw_id", "source_post_url"],
    )


def _link_existing_commenters_from_creator_snapshot(
    db: Session,
    brand_raw_id: int,
    creator_username: str,
) -> None:
    """
    Link a creator's previously-discovered commenters to a new brand
    without re-scraping. Reads from instagram_creator_commenters directly
    (the real relational link table) rather than a creator's legacy
    top_posts JSONB snapshot, since a creator can now own multiple rows
    (one per post) with no single "the" row to read a snapshot from —
    querying every id sharing this username covers all of them regardless.
    """
    creator_ids = [
        r.id for r in
        db.query(InstagramUser.id).filter(InstagramUser.username == creator_username).all()
    ]
    if not creator_ids:
        return

    links = (
        db.query(InstagramCreatorCommenter, InstagramUser.username)
        .join(InstagramUser, InstagramUser.id == InstagramCreatorCommenter.commenter_user_id)
        .filter(InstagramCreatorCommenter.creator_user_id.in_(creator_ids))
        .all()
    )
    for link, commenter_username in links:
        _link_to_brand(db, brand_raw_id, commenter_username)
        _link_commenter_to_creator(db, brand_raw_id, creator_username, {
            "username": commenter_username,
            "source_post_url": link.source_post_url,
            "comment_text": link.comment_text,
            "comment_likes": link.comment_likes,
        })


#  Main enrichment function 

def enrich_instagram_users(
    db: Session,
    limit: int = 5,
    row_id: int | None = None,
    brand_raw_id: int | None = None,
) -> int:
    """
    Process up to `limit` instagram_posts where is_users_scraped=False.
    Returns number of posts processed.

    Pass row_id to target one specific instagram_posts row by its primary
    key (instagram_posts.id) — bypasses the is_users_scraped filter (so you
    can re-run/test a post that was already processed). Not to be confused
    with instagram_posts.post_id, which is Instagram's own post ID string.

    Pass brand_raw_id to scope the run to one brand's instagram_posts rows
    instead — still filtered to is_users_scraped=False, so repeated calls
    (e.g. limit=1 in a loop) advance through that brand's pending posts one
    at a time rather than reprocessing the same already-done row forever.
    Ignored if row_id is given.
    """
    if not APIFY_TOKEN:
        logger.warning("APIFY_TOKEN not set — skipping Instagram user enrichment")
        return 0

    query = db.query(InstagramPost)
    if row_id is not None:
        query = query.filter(InstagramPost.id == row_id)
    elif brand_raw_id is not None:
        query = query.filter(
            InstagramPost.brand_raw_id == brand_raw_id,
            InstagramPost.is_users_scraped == False,
        )
    else:
        query = query.filter(InstagramPost.is_users_scraped == False)

    posts: list[InstagramPost] = query.limit(limit).all()

    if not posts:
        logger.info("Instagram users: no pending posts")
        return 0

    logger.info("Instagram users: processing %d post(s)", len(posts))
    processed = 0

    try:
        for post in posts:
            creators = _collect_creators(post)   # {username: user_type}

            if not creators:
                post.is_users_scraped = True
                db.commit()
                processed += 1
                continue

            # Set once any _scrape_posts() call for this post fails (actor limit
            # reached, network error, etc.) — gates is_users_scraped below so a
            # failed Apify run isn't indistinguishable from "nothing to scrape"
            # and doesn't get silently treated as fully processed.
            scrape_failed = False

            # Filter creators already in DB
            existing = {
                r.username for r in
                db.query(InstagramUser.username)
                .filter(InstagramUser.username.in_(creators))
                .all()
            }
            new_creators = {u: t for u, t in creators.items() if u not in existing}
            for username in existing:
                _link_to_brand(db, post.brand_raw_id, username)
                _link_existing_commenters_from_creator_snapshot(db, post.brand_raw_id, username)

            logger.info(
                "Instagram users: post %s → %d creator(s) (%d new, %d already in DB)",
                post.post_id, len(creators), len(new_creators), len(existing),
            )

            for username, user_type in new_creators.items():
                logger.info("Instagram users: scraping creator @%s (%s)", username, user_type)

                #  a. Scrape top 5 posts (addParentData gets profile info too)
                raw_posts = _scrape_posts(username, n=5)
                if raw_posts is None:
                    logger.warning("Instagram users: Apify scrape failed for @%s — will retry", username)
                    scrape_failed = True
                    time.sleep(0.5)
                    continue
                if not raw_posts:
                    logger.warning("Instagram users: no posts returned for @%s", username)
                    time.sleep(0.5)
                    continue

                #  b. Extract profile data from first post's parent fields
                profile = _profile_from_posts(raw_posts)

                #  c. Classify demographics + niche via LLM (niche: creators only)
                demo  = _classify_demographics(db, username, profile)
                time.sleep(0.3)
                niche = _classify_niche(db, username, profile, raw_posts)
                time.sleep(0.3)

                #  d. Store content creator in instagram_users — one row per post
                creator_rows = [
                    _build_post_row(username, user_type, profile, demo, item, niche=niche)
                    for item in raw_posts
                    if item.get("id")
                ]
                upsert_rows(db, InstagramUser, creator_rows, ["post_id"])
                _link_to_brand(db, post.brand_raw_id, username)

                logger.info(
                    "Instagram users: @%s stored — type=%s gender=%s country=%s age=%s niche=%s followers=%s",
                    username, user_type,
                    demo["gender"], demo["country"], demo["age_group"], niche,
                    profile.get("followersCount"),
                )

                #  e. Collect unique commenters from the 5 posts (up to 5/post)
                commenter_records = _collect_commenter_records(raw_posts, n_per_post=5)
                commenter_usernames = sorted({record["username"] for record in commenter_records})
                if not commenter_usernames:
                    time.sleep(1.0)
                    continue

                # Filter commenters already in DB
                existing_c = {
                    r.username for r in
                    db.query(InstagramUser.username)
                    .filter(InstagramUser.username.in_(commenter_usernames))
                    .all()
                }
                new_commenters = [u for u in commenter_usernames if u not in existing_c]
                for commenter in existing_c:
                    _link_to_brand(db, post.brand_raw_id, commenter)
                    for record in commenter_records:
                        if record["username"] == commenter:
                            _link_commenter_to_creator(db, post.brand_raw_id, username, record)

                logger.info(
                    "Instagram users: @%s has %d unique commenter(s) (%d new)",
                    username, len(commenter_usernames), len(new_commenters),
                )

                #  f. Scrape 1 post per commenter for profile data
                for commenter in new_commenters:
                    logger.info("Instagram users:   commenter @%s", commenter)

                    c_posts = _scrape_posts(commenter, n=1)
                    if c_posts is None:
                        logger.warning("Instagram users: Apify scrape failed for commenter @%s — will retry", commenter)
                        scrape_failed = True
                        time.sleep(0.5)
                        continue
                    if not c_posts:
                        time.sleep(0.5)
                        continue

                    c_profile = _profile_from_posts(c_posts)
                    c_demo    = _classify_demographics(db, commenter, c_profile)
                    time.sleep(0.3)

                    if c_posts[0].get("id"):
                        # No conflict target: this row could violate EITHER the
                        # partial username-where-commenter index OR the global
                        # post_id unique constraint (this commenter's own most
                        # recent post may already be stored under a different
                        # username's creator row) — see upsert_rows' docstring.
                        upsert_rows(db, InstagramUser, [
                            _build_post_row(commenter, "commenter", c_profile, c_demo, c_posts[0])
                        ], None)
                    _link_to_brand(db, post.brand_raw_id, commenter)
                    for record in commenter_records:
                        if record["username"] == commenter:
                            _link_commenter_to_creator(db, post.brand_raw_id, username, record)

                    logger.info(
                        "Instagram users:   commenter @%s stored — gender=%s country=%s",
                        commenter, c_demo["gender"], c_demo["country"],
                    )
                    time.sleep(1.0)

                time.sleep(1.0)

            if scrape_failed:
                db.commit()  # keep whatever rows were already stored above
                logger.warning(
                    "Instagram users: post %s — at least one Apify scrape failed — "
                    "leaving is_users_scraped=False so this post is retried instead "
                    "of being treated as fully processed",
                    post.post_id,
                )
                continue

            post.is_users_scraped = True
            db.commit()
            processed += 1
            logger.info("Instagram users: post %s done", post.post_id)
    except ApifyQuotaExceeded as exc:
        logger.error(
            "Instagram users: %s — stopping this run early after %d post(s) processed; "
            "remaining posts stay is_users_scraped=False for the next run",
            exc, processed,
        )

    logger.info("Instagram users: %d post(s) processed", processed)
    return processed
