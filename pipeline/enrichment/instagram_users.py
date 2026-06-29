"""
pipeline/enrichment/instagram_users.py

For each instagram_post where is_users_scraped=False:

  1. Collect unique usernames from coauthor_producers, tagged_users, mentions.
     Each username gets a user_type label (priority: coauthor_producer > tagged_user > mention).

  2. Skip any username already present in instagram_users.

  3. For each new content creator username:
     a. Scrape their top 5 posts via Apify (addParentData=True embeds profile data in every post).
     b. Extract profile data (bio, businessAddress, etc.) from the first post's parent fields.
     c. Classify demographics via Mistral (gender, country, language, location, age_group).
     d. Store in instagram_users with the appropriate user_type.
     e. Collect up to 5 commenters per post from latestComments → up to 25 unique usernames.
     f. For each commenter NOT already in instagram_users:
        - Scrape 1 post via Apify (addParentData=True) to get their profile data.
        - Classify demographics via Mistral.
        - Store in instagram_users with user_type="commenter".

  4. Mark instagram_post.is_users_scraped=True.

Prompt (editable via /admin > Prompts):
  instagram_user_demographics  — used for both content creators and commenters
"""

import logging
import time

from sqlalchemy.orm import Session

from config import APIFY_TOKEN, MISTRAL_API_KEY
from pipeline.db import BrandInstagramUser, InstagramPost, InstagramUser, Prompt
from pipeline.helpers.apify import run_apify_actor
from pipeline.helpers.db import upsert_rows
from pipeline.helpers.llm import call_mistral_json, fill_template

logger = logging.getLogger(__name__)

_ACTOR_ID = "shu8hvrXbJbY3Eb9W"

#  Prompt 

DEMOGRAPHICS_PROMPT_NAME = "instagram_user_demographics"
DEMOGRAPHICS_DEFAULT_PROMPT = """\
You are classifying the demographics of an Instagram user based on their profile.

Username: {username}
Full name: {full_name}
Bio: {bio}
External URL: {external_url}
Business address: {business_address}

Use clues from the bio language, location mentions, name origin, linked website, or business address.

Classify each field:
1. gender       — "male", "female", or "unknown"
2. country      — most likely country (e.g. "south korea", "united states") — or "unknown"
3. language     — primary language in bio (e.g. "english", "korean", "spanish") — or "unknown"
4. location     — specific city or region if mentioned — or "unknown"
5. age_group    — "teen" (13-17), "young_adult" (18-25), "adult" (26-35), "middle_aged" (36-50), "senior" (50+), or "unknown"

Reply ONLY with a JSON object, no extra text:
{"gender": "...", "country": "...", "language": "...", "location": "...", "age_group": "..."}\
"""


def _get_demographics_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == DEMOGRAPHICS_PROMPT_NAME).first()
    return row.content if row else DEMOGRAPHICS_DEFAULT_PROMPT


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
        u = entry.get("username") if isinstance(entry, dict) else None
        if u and not _is_brand(u):
            result[u] = "tagged_user"

    for entry in post.coauthor_producers or []:
        u = entry.get("username") if isinstance(entry, dict) else None
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


#  Apify helpers 

def _scrape_posts(username: str, n: int = 5) -> list[dict]:
    """
    Scrape n posts for a profile. addParentData=True embeds profile fields
    (fullName, biography, externalUrl, followersCount, businessAddress, etc.)
    into every post item so one call gets both posts AND profile data.
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
    )
    return (items or [])[:n]


def _profile_from_posts(posts: list[dict]) -> dict:
    """Extract profile fields embedded by addParentData from the first post item."""
    if not posts:
        return {}
    p = posts[0]
    return {
        "fullName":          p.get("fullName"),
        "biography":         p.get("biography"),
        "externalUrl":       p.get("externalUrl"),
        "businessAddress":   p.get("businessAddress"),
        "followersCount":    p.get("followersCount"),
        "followsCount":      p.get("followsCount"),
        "postsCount":        p.get("postsCount"),
        "verified":          p.get("verified") or p.get("isVerified"),
        "isBusinessAccount": p.get("isBusinessAccount"),
    }


def _top_comments(post_item: dict, n: int = 5) -> list[dict]:
    """Top n comments from a post item, sorted by likes descending."""
    comments = post_item.get("latestComments") or []
    by_likes = sorted(comments, key=lambda c: c.get("likesCount") or 0, reverse=True)
    return [
        {
            "username": c.get("ownerUsername", ""),
            "text":     c.get("text", ""),
            "likes":    c.get("likesCount", 0),
        }
        for c in by_likes[:n]
    ]


def _format_top_posts(raw_posts: list[dict]) -> list[dict] | None:
    """Convert raw Apify post items to the structure stored in top_posts JSONB."""
    if not raw_posts:
        return None
    return [
        {
            "post_id":        str(p.get("id", "")),
            "post_url":       p.get("url") or p.get("displayUrl", ""),
            "caption":        (p.get("caption") or "")[:300],
            "likes_count":    p.get("likesCount", 0),
            "comments_count": p.get("commentsCount", 0),
            "timestamp":      p.get("timestamp", ""),
            "top_comments":   _top_comments(p, n=5),
        }
        for p in raw_posts
    ]


#  LLM helper 

_UNKNOWN_DEMO = {
    "gender": "unknown", "country": "unknown",
    "language": "unknown", "location": "unknown", "age_group": "unknown",
}


def _classify_demographics(db: Session, username: str, profile: dict) -> dict:
    """Classify demographics for a user profile via Mistral."""
    if not MISTRAL_API_KEY:
        return _UNKNOWN_DEMO

    prompt = fill_template(
        _get_demographics_prompt(db),
        username=username,
        full_name=profile.get("fullName") or "",
        bio=profile.get("biography") or "",
        external_url=profile.get("externalUrl") or "",
        business_address=profile.get("businessAddress") or "",
    )
    result = call_mistral_json(prompt, context=f"demographics @{username}")
    if not isinstance(result, dict):
        return _UNKNOWN_DEMO
    return {
        "gender":    result.get("gender", "unknown"),
        "country":   result.get("country", "unknown"),
        "language":  result.get("language", "unknown"),
        "location":  result.get("location", "unknown"),
        "age_group": result.get("age_group", "unknown"),
    }


#  Row builder 

def _build_user_row(
    username: str,
    user_type: str,
    profile: dict,
    demo: dict,
    top_posts: list[dict] | None,
) -> dict:
    return {
        "username":            username,
        "profile_url":         f"https://www.instagram.com/{username}/",
        "user_type":           user_type,
        "full_name":           profile.get("fullName"),
        "bio":                 profile.get("biography"),
        "external_url":        profile.get("externalUrl"),
        "followers_count":     profile.get("followersCount"),
        "follows_count":       profile.get("followsCount"),
        "posts_count":         profile.get("postsCount"),
        "is_verified":         profile.get("verified"),
        "is_business_account": profile.get("isBusinessAccount"),
        "gender":              demo["gender"],
        "country":             demo["country"],
        "language":            demo["language"],
        "location":            demo["location"],
        "age_group":           demo["age_group"],
        "top_posts":           top_posts,
        "raw_profile":         profile,
    }


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


#  Main enrichment function 

def enrich_instagram_users(db: Session, limit: int = 5) -> int:
    """
    Process up to `limit` instagram_posts where is_users_scraped=False.
    Returns number of posts processed.
    """
    if not APIFY_TOKEN:
        logger.warning("APIFY_TOKEN not set — skipping Instagram user enrichment")
        return 0

    posts: list[InstagramPost] = (
        db.query(InstagramPost)
        .filter(InstagramPost.is_users_scraped == False)
        .limit(limit)
        .all()
    )

    if not posts:
        logger.info("Instagram users: no pending posts")
        return 0

    logger.info("Instagram users: processing %d post(s)", len(posts))
    processed = 0

    for post in posts:
        creators = _collect_creators(post)   # {username: user_type}

        if not creators:
            post.is_users_scraped = True
            db.commit()
            processed += 1
            continue

        # Filter creators already in DB
        existing = {
            r.username for r in
            db.query(InstagramUser.username)
            .filter(InstagramUser.username.in_(creators))
            .all()
        }
        new_creators = {u: t for u, t in creators.items() if u not in existing}

        logger.info(
            "Instagram users: post %s → %d creator(s) (%d new, %d already in DB)",
            post.post_id, len(creators), len(new_creators), len(existing),
        )

        for username, user_type in new_creators.items():
            logger.info("Instagram users: scraping creator @%s (%s)", username, user_type)

            #  a. Scrape top 5 posts (addParentData gets profile info too) 
            raw_posts = _scrape_posts(username, n=5)
            if not raw_posts:
                logger.warning("Instagram users: no posts returned for @%s", username)
                time.sleep(0.5)
                continue

            #  b. Extract profile data from first post's parent fields 
            profile = _profile_from_posts(raw_posts)

            #  c. Classify demographics via LLM 
            demo = _classify_demographics(db, username, profile)
            time.sleep(0.3)

            #  d. Store content creator in instagram_users 
            top_posts = _format_top_posts(raw_posts)
            upsert_rows(db, InstagramUser, [
                _build_user_row(username, user_type, profile, demo, top_posts)
            ], ["username"])
            _link_to_brand(db, post.brand_raw_id, username)

            logger.info(
                "Instagram users: @%s stored — type=%s gender=%s country=%s age=%s followers=%s",
                username, user_type,
                demo["gender"], demo["country"], demo["age_group"],
                profile.get("followersCount"),
            )

            #  e. Collect unique commenters from the 5 posts (up to 5/post) 
            commenter_usernames = _collect_commenters(raw_posts, n_per_post=5)
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

            logger.info(
                "Instagram users: @%s has %d unique commenter(s) (%d new)",
                username, len(commenter_usernames), len(new_commenters),
            )

            #  f. Scrape 1 post per commenter for profile data 
            for commenter in new_commenters:
                logger.info("Instagram users:   commenter @%s", commenter)

                c_posts = _scrape_posts(commenter, n=1)
                if not c_posts:
                    time.sleep(0.5)
                    continue

                c_profile  = _profile_from_posts(c_posts)
                c_demo     = _classify_demographics(db, commenter, c_profile)
                c_top_posts = _format_top_posts(c_posts)
                time.sleep(0.3)

                upsert_rows(db, InstagramUser, [
                    _build_user_row(commenter, "commenter", c_profile, c_demo, c_top_posts)
                ], ["username"])
                _link_to_brand(db, post.brand_raw_id, commenter)

                logger.info(
                    "Instagram users:   commenter @%s stored — gender=%s country=%s",
                    commenter, c_demo["gender"], c_demo["country"],
                )
                time.sleep(1.0)

            time.sleep(1.0)

        post.is_users_scraped = True
        db.commit()
        processed += 1
        logger.info("Instagram users: post %s done", post.post_id)

    logger.info("Instagram users: %d post(s) processed", processed)
    return processed
