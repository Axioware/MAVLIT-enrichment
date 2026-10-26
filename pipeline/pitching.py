"""
Generates a personal, relationship-building outreach pitch for a creator to
send a brand (existing brands_raw row, or a brand they typed in themselves),
and persists it as a generated, unsent Pitch row.
"""

import logging
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from pipeline.brand_detail import _top_contact
from pipeline.db import BrandRaw, CreatorProfile, InstagramPost, Pitch, Prompt
from pipeline.helpers.creator_gender import creator_match_gender
from pipeline.helpers.social import normalize_handle
from pipeline.pitch_posts import describe_entries, has_post_urls, resolve_post_details
from pipeline.helpers.gpt_llm import call_gpt_text, fill_template, llm_usage_scope, stream_gpt_text
from pipeline.helpers.prompts import (
    PITCH_DEFAULT_PROMPT,
    PITCH_PRODUCT_INPUT_LINE,
    PITCH_POSTS_RULE,
    PITCH_PRODUCT_RULE,
    PITCH_PROMPT_NAME,
    PITCH_STYLE_BLOCK,
    PITCH_VIEWS_LINE,
    PITCH_VIEWS_RULE,
)

logger = logging.getLogger(__name__)


def _get_pitch_prompt(db: Session) -> str:
    row = db.query(Prompt).filter(Prompt.name == PITCH_PROMPT_NAME).first()
    return row.content if row else PITCH_DEFAULT_PROMPT


_PARTNER_CREATORS_MAX = 2
_MIN_PARTNERSHIP_CONFIDENCE = 90

# Creators with a confirmed (sponsorship_confidence >= 90) partnership post with the brand:
# (a) usernames tagged / mentioned / co-authoring / sponsoring on the brand's own
# instagram_posts, looked up in instagram_users, and (b) the creators behind the brand's
# test_creator_brand_partnership_posts. Same sources as brand_signals / the niche matcher.
# One row per creator (their best, latest post), strongest partnership first.
_PARTNER_CREATORS_SQL = r"""
WITH brand AS (
  SELECT lower(regexp_replace(regexp_replace(btrim(coalesce(instagram_handle, '')),
         '^(https?://)?(www\.)?instagram\.com/', '', 'i'), '[/?#].*$|^@', '', 'g')) AS handle
  FROM brands_raw WHERE id = :bid
),
hi AS (
  SELECT p.post_url, p.timestamp, p.caption, p.sponsorship_confidence,
         p.tagged_users, p.mentions, p.coauthor_producers, p.sponsors,
         p.likes_count, coalesce(p.video_view_count, p.video_play_count) AS views
  FROM instagram_posts p
  WHERE p.brand_raw_id = :bid AND p.post_id IS NOT NULL AND p.sponsorship_confidence >= :min_conf
),
names AS (
  SELECT hi.post_url, hi.timestamp, hi.caption, hi.sponsorship_confidence, hi.likes_count, hi.views,
         lower(CASE WHEN jsonb_typeof(e) = 'string' THEN e #>> '{}' ELSE e ->> 'username' END) AS username
  FROM hi
  CROSS JOIN LATERAL (
    SELECT jsonb_array_elements(CASE WHEN jsonb_typeof(col) = 'array' THEN col ELSE '[]'::jsonb END) AS e
    FROM (VALUES (hi.tagged_users), (hi.mentions), (hi.coauthor_producers), (hi.sponsors)) AS t(col)
  ) x
),
from_posts AS (
  SELECT n.username, n.post_url, n.timestamp AS posted_at, n.caption, n.sponsorship_confidence, n.likes_count, n.views
  FROM names n, brand b
  WHERE n.username IS NOT NULL AND n.username <> '' AND n.username <> b.handle
),
from_re AS (
  SELECT lower(btrim(t.creator_username)) AS username, t.post_url, t.post_timestamp AS posted_at,
         t.caption, t.sponsorship_confidence,
         -- the same post as a per-post row of instagram_users (likes only: that table stores no views)
         (SELECT u.likes_count FROM instagram_users u WHERE t.post_id IS NOT NULL AND u.post_id = t.post_id LIMIT 1) AS likes_count,
         NULL::int AS views
  FROM test_creator_brand_partnership_posts t
  WHERE t.brand_raw_id = :bid AND t.sponsorship_confidence >= :min_conf
),
partners AS (SELECT * FROM from_posts UNION ALL SELECT * FROM from_re),
profile AS (
  SELECT lower(u.username) AS username,
         max(u.followers_count) AS followers,
         max(lower(btrim(u.niche))) FILTER (WHERE u.niche IS NOT NULL AND btrim(u.niche) <> '') AS niche,
         max(lower(btrim(u.gender))) FILTER (WHERE u.gender IS NOT NULL AND btrim(u.gender) <> '') AS gender
  FROM instagram_users u
  WHERE u.user_type <> 'commenter'
  GROUP BY lower(u.username)
),
re_niche AS (
  SELECT lower(btrim(c.username)) AS username, max(lower(btrim(c.niche))) AS niche
  FROM content_creator_re c WHERE c.niche IS NOT NULL AND btrim(c.niche) <> '' GROUP BY 1
)
SELECT DISTINCT ON (pa.username)
       pa.username, pr.followers, coalesce(pr.niche, rn.niche) AS niche, pr.gender,
       pa.post_url, pa.posted_at, pa.caption, pa.sponsorship_confidence, pa.likes_count, pa.views
FROM partners pa
LEFT JOIN profile pr ON pr.username = pa.username
LEFT JOIN re_niche rn ON rn.username = pa.username
WHERE coalesce(pr.niche, rn.niche) = ANY(:niches)
ORDER BY pa.username, pa.sponsorship_confidence DESC NULLS LAST, (pa.likes_count IS NULL), pa.posted_at DESC NULLS LAST
"""


def _creator_niches(creator: CreatorProfile) -> list[str]:
    """The writer's niche(s), lower-case: comma-separated niche fields of the profile."""
    values = (creator.instagram_primary_niche, creator.content_niche)
    return sorted({n.strip().lower() for v in values for n in (v or "").split(",") if n.strip()})


def partner_creators(db: Session, brand_id: int, creator: CreatorProfile) -> list[dict]:
    """
    At most _PARTNER_CREATORS_MAX creators with a confirmed (>= 90) partnership with the
    brand whose niche is the same as the writer's niche. [] when the writer has no niche or
    no such creator exists.
    """
    niches = _creator_niches(creator)
    if not niches:
        return []
    rows = db.execute(
        text(_PARTNER_CREATORS_SQL),
        {"bid": brand_id, "min_conf": _MIN_PARTNERSHIP_CONFIDENCE, "niches": niches},
    ).mappings().all()
    ranked = sorted((dict(r) for r in rows), key=lambda r: str(r["posted_at"] or ""), reverse=True)   # newest first
    ranked.sort(key=lambda r: r["sponsorship_confidence"] or 0, reverse=True)                        # then strongest first
    return ranked[:_PARTNER_CREATORS_MAX]


def _partner_creators_text(creators: list[dict]) -> str:
    if not creators:
        return "none on file"
    lines = []
    for i, c in enumerate(creators, 1):
        parts = [f"@{c['username']}", f"niche: {c['niche']}"]
        if c.get("followers"):
            parts.append(f"{c['followers']:,} followers")
        if c.get("gender"):
            parts.append(f"gender: {c['gender']}")
        if c.get("posted_at"):
            parts.append(f"sponsored post on {str(c['posted_at'])[:10]}")
        caption = " ".join((c.get("caption") or "").split())[:220]
        if caption:
            parts.append(f'post caption: "{caption}"')
        lines.append(f"{i}. " + "; ".join(parts))
    return "\n".join(lines)


def _brand_audience_text(brand: BrandRaw) -> tuple[str, str]:
    """(target audience, target age) for the prompt; the age is an unconfirmed estimate."""
    gender = (brand.target_audience_gender or "").strip().lower()
    audience = {"male": "mostly men", "female": "mostly women", "both": "men and women"}.get(gender)
    explanation = " ".join((brand.audience_analysis_explanation or "").split())[:300]
    audience_text = "; ".join(x for x in (audience, explanation) if x) or "not known"
    low, high = brand.target_audience_min_age, brand.target_audience_max_age
    if low and high:
        age = f"around {low} to {high}"
    elif low or high:
        age = f"around {low or high}"
    else:
        age = "not known"
    return audience_text, age


def _brand_instagram_followers(db: Session, brand_id: int) -> str:
    row = (
        db.query(InstagramPost.followers_count)
        .filter(InstagramPost.brand_raw_id == brand_id, InstagramPost.followers_count.isnot(None))
        .order_by(InstagramPost.fetched_at.desc())
        .first()
    )
    return f"{row[0]:,}" if row else "unknown"


def _build_pitch_prompt(
    db: Session,
    creator: CreatorProfile,
    *,
    is_custom: bool,
    brand_id: int | None,
    custom_brand_name: str | None,
    story: str,
    product_reference: str | None,
    past_brand_partnership: str | None,
    content_link: str | None,
    post_details: dict | None = None,
) -> dict:
    """
    The filled-in pitch prompt plus the brand/contact it was built for:
    {"prompt", "brand", "brand_name", "contact"}. Writes nothing. Raises ValueError if
    brand_id doesn't exist.
    """
    brand: BrandRaw | None = None
    contact: dict | None = None
    if is_custom:
        brand_name = custom_brand_name
        brand_niche = "unknown"
        brand_description = "none provided"
        contact_name, contact_title = "unknown", "unknown"
        brand_target_audience, brand_target_age = "not known", "not known"
        brand_followers, partners_text = "unknown", "none on file"
    else:
        brand = db.query(BrandRaw).filter(BrandRaw.id == brand_id).first()
        if not brand:
            raise ValueError(f"brand_id={brand_id} not found")
        # own_brand.py keeps names unique by adding " (@handle)"; the email uses the plain name
        brand_name = (
            re.sub(r"\s*\(@[A-Za-z0-9._]+\)$", "", brand.name or "")
            or normalize_handle(brand.instagram_handle or "")
            or "this brand"
        )
        brand_niche = brand.niche or "unknown"
        brand_description = brand.description or "none provided"
        contact = _top_contact(db, brand_id)
        contact_name = contact["name"] if contact else "unknown"
        contact_title = contact["title"] if contact else "unknown"
        brand_target_audience, brand_target_age = _brand_audience_text(brand)
        brand_followers = _brand_instagram_followers(db, brand.id)
        partners_text = _partner_creators_text(partner_creators(db, brand.id, creator))

    template = _get_pitch_prompt(db)
    style = (creator.pitch_style_preferences or "").strip()
    if style and "{pitch_style_preferences}" not in template:
        # an edited stored copy without the placeholder: still honour the creator's preferences
        template = template.rstrip() + "\n\n" + PITCH_STYLE_BLOCK
    if (product_reference or "").strip() and "{product_reference}" not in template:
        # an edited stored copy without the placeholder: still use the product launch
        template = template.rstrip() + "\n\n" + PITCH_PRODUCT_INPUT_LINE + PITCH_PRODUCT_RULE
    if creator.instagram_highest_views and "{creator_highest_views}" not in template:
        # an edited stored copy without the placeholder: still give the LLM the creator's highest views
        template = template.rstrip() + "\n\n" + PITCH_VIEWS_LINE + PITCH_VIEWS_RULE
    # Instagram post URLs in the past partnerships / content links become url + caption + views (saved posts are
    # reused, only unknown ones are scraped). The stream endpoint resolves them first to show progress.
    if post_details is None:
        post_details = resolve_post_details(db, creator, past_brand_partnership, content_link)
    past_text = describe_entries(db, creator, past_brand_partnership, post_details)
    link_text = describe_entries(db, creator, content_link, post_details)
    if post_details and PITCH_POSTS_RULE not in template:
        template = template.rstrip() + "\n\n" + PITCH_POSTS_RULE
    prompt = fill_template(
        template,
        creator_highest_views=f"{creator.instagram_highest_views:,}" if creator.instagram_highest_views else "unknown",
        pitch_style_preferences=style or "none",
        product_reference=(product_reference or "").strip() or "none provided",
        creator_name=creator.full_name or "unknown",
        creator_handle=creator.instagram_handle or creator.creator_handle or "unknown",
        creator_niche=creator.content_niche or "unknown",
        creator_sub_niches=", ".join(creator.instagram_sub_niches or []) or "none provided",
        creator_follower_count=f"{creator.follower_count:,}" if creator.follower_count else "unknown",
        creator_audience_gender=creator_match_gender(creator) or "not specified",
        creator_content_description=(creator.instagram_description or "none provided").strip(),
        creator_audience_description=(creator.instagram_audience_description or "none provided").strip(),
        brand_name=brand_name or "unknown",
        brand_niche=brand_niche,
        brand_description=" ".join((brand_description or "none provided").split())[:900],
        brand_target_audience=brand_target_audience,
        brand_target_age=brand_target_age,
        brand_instagram_followers=brand_followers,
        brand_partner_creators=partners_text,
        contact_name=contact_name,
        contact_title=contact_title,
        story=(story or "").strip() or "none provided",
        past_brand_partnership=past_text or "none provided",
        content_link=link_text or "none provided",
    )
    return {"prompt": prompt, "brand": brand, "brand_name": brand_name, "contact": contact}


def _save_pitch(
    db: Session, creator: CreatorProfile, built: dict, pitch_text: str, *, is_custom: bool, story: str,
    product_reference: str | None, past_brand_partnership: str | None, content_link: str | None,
) -> Pitch:
    brand, contact = built["brand"], built["contact"]
    pitch = Pitch(
        creator_profile_id=creator.id,
        brand_raw_id=brand.id if brand else None,
        is_custom=is_custom,
        brand_name=built["brand_name"],
        story=story,
        product_reference=product_reference,
        past_brand_partnership=past_brand_partnership,
        content_link=content_link,
        contact_name=contact["name"] if contact else None,
        contact_email=contact["email"] if contact else None,
        pitch_text=pitch_text,
        status="generated",
    )
    db.add(pitch)
    db.commit()
    db.refresh(pitch)
    logger.info("Pitch generated: creator_id=%d brand=%s pitch_id=%d", creator.id, built["brand_name"], pitch.id)
    return pitch


def generate_pitch(
    db: Session,
    creator: CreatorProfile,
    *,
    is_custom: bool,
    brand_id: int | None,
    custom_brand_name: str | None,
    story: str,
    product_reference: str | None,
    past_brand_partnership: str | None,
    content_link: str | None,
) -> Pitch:
    """
    Raises ValueError if brand_id doesn't exist. Raises RuntimeError if the
    LLM call comes back empty — callers should not persist a blank pitch as
    "sent", so nothing is written to the DB in that case.
    """
    fields = dict(story=story, product_reference=product_reference,
                  past_brand_partnership=past_brand_partnership, content_link=content_link)
    built = _build_pitch_prompt(db, creator, is_custom=is_custom, brand_id=brand_id,
                                custom_brand_name=custom_brand_name, **fields)
    with llm_usage_scope(creator.id, "pitch"):
        pitch_text = call_gpt_text(built["prompt"], context=f"pitch for creator_id={creator.id} brand={built['brand_name']}")
    if not pitch_text:
        raise RuntimeError("Pitch generation failed — LLM returned no content")
    return _save_pitch(db, creator, built, pitch_text, is_custom=is_custom, **fields)


def stream_pitch(
    db: Session,
    creator: CreatorProfile,
    *,
    is_custom: bool,
    brand_id: int | None,
    custom_brand_name: str | None,
    story: str,
    product_reference: str | None,
    past_brand_partnership: str | None,
    content_link: str | None,
):
    """
    Same as generate_pitch, but yields the email piece by piece while the LLM writes it (so the
    page can show it live), then saves it. Yields ("status", text) first when Instagram posts have to be
    read, ("delta", text) for every piece and, last, ("done", Pitch). The Pitch is saved only after the
    full text arrived; if the stream fails nothing is saved and the exception (ValueError /
    LLMUnavailableError / RuntimeError) is raised.
    """
    fields = dict(story=story, product_reference=product_reference,
                  past_brand_partnership=past_brand_partnership, content_link=content_link)
    post_details = None
    if has_post_urls(past_brand_partnership, content_link):
        yield "status", "Reading the Instagram posts you linked…"   # may need an Apify run
        post_details = resolve_post_details(db, creator, past_brand_partnership, content_link)
    yield "status", "Writing your pitch…"
    built = _build_pitch_prompt(db, creator, is_custom=is_custom, brand_id=brand_id,
                                custom_brand_name=custom_brand_name, post_details=post_details, **fields)
    parts: list[str] = []
    for piece in stream_gpt_text(
        built["prompt"], context=f"pitch for creator_id={creator.id} brand={built['brand_name']}",
        scope=(creator.id, "pitch"),
    ):
        parts.append(piece)
        yield "delta", piece
    pitch_text = "".join(parts).strip()
    if not pitch_text:
        raise RuntimeError("Pitch generation failed — LLM returned no content")
    yield "done", _save_pitch(db, creator, built, pitch_text, is_custom=is_custom, **fields)
