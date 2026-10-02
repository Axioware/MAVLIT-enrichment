"""Classify audience demographics for qualified brands using website evidence.

Run from the project root:
    python -m gender_check.run_brand_website_audience_check --limit 1
    python -m gender_check.run_brand_website_audience_check --brand-id 2151
    python -m gender_check.run_brand_website_audience_check --brand-id 2151 --force

Use --dry-run to list qualifying brands without scraping or calling the LLM.
Use --force (only with --brand-id) to re-analyse brands that already have
audience results; their existing values are overwritten.
"""

import argparse
import json
import logging
import math
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from sqlalchemy import text

from pipeline.db import (
    BrandRaw,
    InstagramPost,
    SessionLocal,
    TestCreatorBrandPartnershipPost,
)
from pipeline.enrichment.geo_reach.geo_reach import (
    _clean_page_text,
    _fetch_page,
    _normalize_origin,
    _registrable_domain,
)
from pipeline.helpers.gpt_llm import call_gpt_json, fill_template

MAX_INSTAGRAM_POSTS_FOR_LLM = 10
LLM_MODEL = "gpt-5"
MAX_WEBSITE_PAGES = 5
AUDIENCE_STOP_CONFIDENCE = 90
PAGE_TEXT_LIMIT = 6000

# Product and audience pages take priority over generic site pages. Other
# internal links remain queued as fallbacks so useful evidence is not missed.
_AUDIENCE_LINK_TERMS = (
    "men", "mens", "man", "male", "beard", "shave", "groom",
    "women", "womens", "woman", "female", "makeup", "cosmetic",
    "skincare", "skin-care", "haircare", "hair-care", "maternity",
    "unisex", "gender", "collection", "product", "shop", "store",
    "catalog", "category", "service", "therapy", "treatment",
    "kids", "kid", "children", "child", "baby", "teen", "youth",
    "junior", "age", "customer", "audience", "about", "our-story",
)

_UNANALYSED_FILTER = """
        AND br.target_audience_gender IS NULL
        AND br.target_audience_gender_confidence IS NULL
        AND br.target_audience_min_age IS NULL
        AND br.target_audience_max_age IS NULL
        AND br.target_audience_age_confidence IS NULL
        AND br.product_audience_gender IS NULL
        AND br.product_audience_gender_confidence IS NULL
        AND br.product_audience_min_age IS NULL
        AND br.product_audience_max_age IS NULL
        AND br.product_audience_age_confidence IS NULL
        AND br.audience_analysis_explanation IS NULL"""

_BRAND_QUERY_SQL = """
    WITH best_per_brand AS (
      SELECT DISTINCT ON (tcbp.brand_raw_id)
        ccr.username AS creator_username,
        ccr.niche,
        tcbp.brand_raw_id,
        tcbp.brand_name,
        tcbp.post_url,
        tcbp.post_timestamp,
        tcbp.sponsorship_confidence,
        br.has_official_website,
        br.website,
        br.description
      FROM content_creator_re ccr
      JOIN test_creator_brand_partnership_posts tcbp
        ON tcbp.content_creator_re_id = ccr.id
      JOIN brands_raw br
        ON br.id = tcbp.brand_raw_id
      WHERE ccr.id BETWEEN 1 AND 208
        AND ccr.niche IN ('Beauty', 'Music', 'Fitness', 'Health')
        AND tcbp.sponsorship_confidence >= 90
        AND br.refferls = false{unanalysed_filter}
        AND (
          br.geo_reach_score BETWEEN 0 AND 40
          OR br.geo_reach_score IS NULL
        )
        AND tcbp.post_timestamp >= '2026-01-01'
        AND tcbp.post_timestamp < '2027-01-01'
        AND (
          SELECT COUNT(*)
          FROM instagram_posts ip
          WHERE ip.brand_raw_id = br.id
        ) >= 5
      ORDER BY
        tcbp.brand_raw_id,
        tcbp.sponsorship_confidence DESC NULLS LAST,
        tcbp.post_timestamp DESC NULLS LAST
    )
    SELECT
      brand_raw_id,
      brand_name,
      creator_username,
      post_url,
      post_timestamp,
      sponsorship_confidence,
      website,
      description
    FROM best_per_brand
    WHERE has_official_website = true
      AND website IS NOT NULL
      AND description IS NOT NULL
    ORDER BY brand_name
"""

# Default: only brands with no audience results yet. --force drops that
# condition so already-analysed brands can be re-run.
BRAND_QUERY = text(_BRAND_QUERY_SQL.replace("{unanalysed_filter}", _UNANALYSED_FILTER))
FORCE_BRAND_QUERY = text(_BRAND_QUERY_SQL.replace("{unanalysed_filter}", ""))

AUDIENCE_PROMPT = """
You are analyzing a brand to determine its audience demographics.

Your goal is to estimate:

1. target audience = who the brand's marketing is trying to attract or persuade.
2. product audience = who the product or service is designed for, used by, received by, or serves.

These can be different.
Do not assume that the buyer and the product user are the same person.

Examples:
- Baby products:
  - target_audience = decide from the partner creators (see "Partner creators" below)
  - product_audience = both (babies)
- Men's grooming:
  - target_audience = male
  - product_audience = male
- Family restaurant:
  - target_audience = both
  - product_audience = both

IMPORTANT RULES

Use evidence in this priority order.

For TARGET audience (gender and age) - who the brand features and speaks to in its marketing:

1. Instagram captions and hashtags, including the partner creators in each post (highest priority)
2. Instagram bio
3. Website text
4. Brand description

For PRODUCT audience (gender and age):

1. Instagram captions and hashtags (highest priority)
2. Instagram bio
3. Website text
4. Brand description

Latest product

Today's date is {today}. Each Instagram post includes its timestamp.

Return a latest_product ONLY if one product meets BOTH conditions:

1. Recently launched: there is explicit launch evidence such as "new", "introducing",
   "just launched", "just dropped", "now available", "new arrival" or a launch campaign,
   and that evidence is recent (within roughly the last 6 months of today's date,
   judged from the post timestamps).
2. Actively marketed: the brand promotes it repeatedly - it appears in at least 2 of the
   Instagram posts, or in at least 1 Instagram post AND as a featured item on the website
   (homepage hero, banner, "new" section).

Do NOT return:
- a product that is only listed in a catalog, shop page or menu without launch evidence
- a product mentioned once in passing
- a product launched long ago, even if it is still promoted
- a guess based on which item looks newest

If no product meets both conditions, return null and confidence 0. It is better to return
null than a weak guess.

Give latest_product_confidence as a whole-number score from 0 to 100 for how strongly the
evidence shows the product is both recently launched and actively marketed. If
latest_product is null, latest_product_confidence must be 0.

On the website, prioritize evidence from product, shop, and collection pages. Gender- or age-specific sections can be linked by paths such as /collections/mens, /collections/womens, /shop/men, /collections/beard-care, /collections/makeup, or /collections/mens-grooming. Use both the page URL/category and its actual contents; do not infer a demographic from a path alone.

Do NOT determine demographics from:
- Brand name alone
- Founder gender
- Models appearing in photos
- Stereotypes about industries

Gender Classification

For each gender field, return exactly one of these JSON values:
- "male"
- "female"
- "both"
- null

Use "both" only when there is positive evidence the brand actively serves both genders.

If evidence is weak, conflicting, or absent:
- return null
- lower confidence

Partner creators (target audience gender)

The creators a brand partners with show who its marketing is aimed at. Use them as evidence for target_audience_gender:
- the creators in each Instagram post's coauthor_producers, sponsors, tagged_users and mentions
- the REVERSE ENGINEERING PARTNER CREATORS list

Judge each creator's gender from their username, display name and the post caption. Skip creators whose gender you cannot tell, and accounts that are brands or businesses rather than people.

- Return "both" for target_audience_gender only if you found BOTH male and female partner creators AND the split is balanced: between 50/50 and 70/30.
- If MORE than 70% of the partner creators whose gender you can tell are one gender, return that gender, not "both". Examples:
  - 5 female + 5 male (50/50) -> "both"
  - 7 female + 3 male (70/30) -> "both"
  - 3 female + 1 male (75/25) -> "female"
  - 4 female + 1 male (80/20) -> "female"
  - 2 female + 0 male -> "female"
- Apply the same ratio rule to any other evidence of who the marketing features, such as the people shown or addressed in Instagram captions or on the website: if one gender makes up more than 70% of it, return that gender, not "both".
- This matters most for niches that look like they target both genders, such as baby, parenting and family brands: words like "parents" or "families" on the website are not enough for "both" on their own.

Age Classification

Estimate realistic age ranges only when supported by evidence.
For each age endpoint, return a whole-number JSON integer from 0 to 120, or null when the endpoint is not supported. If only one endpoint is supported, return that endpoint and null for the other. Do not return ages as strings or decimals.

Strong evidence includes:
- Explicit age references
- Product positioning
- Customer descriptions
- Educational level references
- Life-stage references

Examples:
- College students → roughly 18-24
- Teen skincare → roughly 13-19
- Professional networking tools → roughly 22-55

Do NOT invent age ranges solely from the industry.

If evidence is insufficient:
- return null

Confidence Guidelines

Return a whole-number JSON integer from 0 to 100 for each of the four confidence fields. Confidence measures how strongly the evidence supports that field, not how strongly the brand prefers a demographic. If both endpoints of an age range are null, its age confidence must be 0.

95-100:
Direct demographic statements on website.

85-94:
Strong repeated evidence across website and social content.

70-84:
Reasonable inference from products, customers, and messaging.

50-69:
Weak inference.

Below 50:
Insufficient evidence. Prefer null.

Explanation Requirements

Maximum 50 words.

Explain:
- Why target audience was selected.
- Why product audience was selected.
- Key website or Instagram evidence.
- Any uncertainty.

Brand name: {brand_name}
Website: {website}
Business description: {description}

SCRAPED WEBSITE TEXT:
{website_text}

INSTAGRAM BIO:
{bio}

INSTAGRAM BUSINESS CATEGORY:
{business_category_name}

RECENT INSTAGRAM POSTS:
{posts}

REVERSE ENGINEERING PARTNER CREATORS (creators with a confirmed paid partnership post for this brand):
{re_creators}

Return ONLY valid JSON with exactly these keys. Use JSON null (without quotes) for unknown gender, age, or latest product values, and integer numbers (without quotes) for ages and confidence scores. Do not include comments, markdown, or extra keys.

{
  "target_audience_gender": null,
  "target_audience_gender_confidence": 0,
  "target_audience_min_age": null,
  "target_audience_max_age": null,
  "target_audience_age_confidence": 0,
  "product_audience_gender": null,
  "product_audience_gender_confidence": 0,
  "product_audience_min_age": null,
  "product_audience_max_age": null,
  "product_audience_age_confidence": 0,
  "latest_product": null,
  "latest_product_confidence": 0,
  "audience_analysis_explanation": ""
}
"""

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _url_key(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.netloc.lower()}{parsed.path.rstrip('/') or '/'}"


def _discover_audience_links(html: str, origin: str) -> list[str]:
    """Rank real internal product/audience links first; retain other links as fallbacks."""
    soup = BeautifulSoup(html, "html.parser")
    origin_domain = _registrable_domain(urlparse(origin).netloc)
    links: list[tuple[int, str]] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        parsed = urlparse(urljoin(origin + "/", href))
        if parsed.scheme not in {"http", "https"} or _registrable_domain(parsed.netloc) != origin_domain:
            continue
        path = parsed.path.rstrip("/") or "/"
        link = f"{parsed.scheme}://{parsed.netloc}{path}"
        key = _url_key(link)
        if key in seen:
            continue
        seen.add(key)
        hints = f"{path} {anchor.get_text(' ', strip=True)}".lower().replace("'", "")
        score = sum(1 for term in _AUDIENCE_LINK_TERMS if term in hints)
        links.append((score, link))
    links.sort(key=lambda item: -item[0])
    return [link for _, link in links]


def _bounded_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def _age(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 120 or not number.is_integer():
        return None
    return int(number)


def _gender(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in {"male", "female", "both"} else None


def _metrics(result: dict) -> dict:
    target_gender = _gender(result.get("target_audience_gender"))
    product_gender = _gender(result.get("product_audience_gender"))
    target_min = _age(result.get("target_audience_min_age"))
    target_max = _age(result.get("target_audience_max_age"))
    product_min = _age(result.get("product_audience_min_age"))
    product_max = _age(result.get("product_audience_max_age"))
    if target_min is not None and target_max is not None and target_min > target_max:
        target_min = target_max = None
    if product_min is not None and product_max is not None and product_min > product_max:
        product_min = product_max = None

    target_gender_conf = _bounded_number(result.get("target_audience_gender_confidence"))
    product_gender_conf = _bounded_number(result.get("product_audience_gender_confidence"))
    target_age_conf = _bounded_number(result.get("target_audience_age_confidence"))
    product_age_conf = _bounded_number(result.get("product_audience_age_confidence"))
    any_estimate = any(value is not None for value in (
        target_gender, product_gender, target_min, target_max, product_min, product_max,
    ))
    explanation = result.get("audience_analysis_explanation")
    latest_product = result.get("latest_product")
    latest_product = latest_product.strip() if isinstance(latest_product, str) and latest_product.strip() else None
    latest_product_conf = _bounded_number(result.get("latest_product_confidence"))

    return {
        "target_audience_gender": target_gender,
        "target_audience_gender_confidence": round(target_gender_conf) if target_gender and target_gender_conf is not None else 0,
        "target_audience_min_age": target_min,
        "target_audience_max_age": target_max,
        "target_audience_age_confidence": round(target_age_conf) if (target_min is not None or target_max is not None) and target_age_conf is not None else 0,
        "product_audience_gender": product_gender,
        "product_audience_gender_confidence": round(product_gender_conf) if product_gender and product_gender_conf is not None else 0,
        "product_audience_min_age": product_min,
        "product_audience_max_age": product_max,
        "product_audience_age_confidence": round(product_age_conf) if (product_min is not None or product_max is not None) and product_age_conf is not None else 0,
        "latest_product": latest_product,
        "latest_product_confidence": round(latest_product_conf) if latest_product and latest_product_conf is not None else 0,
        "audience_analysis_explanation": explanation.strip() if isinstance(explanation, str) and explanation.strip() else (
            "No valid audience estimates were returned." if not any_estimate else "No explanation provided."
        ),
    }


def _merge_metrics(previous: dict | None, current: dict) -> dict:
    """Keep the better-supported estimate for each dimension across pages."""
    if not previous:
        return current
    merged = dict(previous)
    dimensions = (
        ("target_audience_gender", "target_audience_gender_confidence"),
        ("product_audience_gender", "product_audience_gender_confidence"),
    )
    for value_key, confidence_key in dimensions:
        if current[value_key] is not None and (
            merged[value_key] is None
            or current[confidence_key] >= merged[confidence_key]
        ):
            merged[value_key] = current[value_key]
            merged[confidence_key] = current[confidence_key]

    for min_key, max_key, confidence_key in (
        ("target_audience_min_age", "target_audience_max_age", "target_audience_age_confidence"),
        ("product_audience_min_age", "product_audience_max_age", "product_audience_age_confidence"),
    ):
        if (current[min_key] is not None or current[max_key] is not None) and (
            (merged[min_key] is None and merged[max_key] is None)
            or current[confidence_key] >= merged[confidence_key]
        ):
            merged[min_key] = current[min_key]
            merged[max_key] = current[max_key]
            merged[confidence_key] = current[confidence_key]

    if current.get("audience_analysis_explanation"):
        merged["audience_analysis_explanation"] = current["audience_analysis_explanation"]
    if current.get("latest_product") is not None and (
        merged.get("latest_product") is None
        or current.get("latest_product_confidence", 0) >= merged.get("latest_product_confidence", 0)
    ):
        merged["latest_product"] = current["latest_product"]
        merged["latest_product_confidence"] = current["latest_product_confidence"]
    return merged


def _has_confident_estimates(metrics: dict) -> bool:
    """Equivalent to geo_reach's confidence gate, applied to all four dimensions."""
    return (
        metrics.get("target_audience_gender") is not None
        and metrics.get("target_audience_gender_confidence", 0) >= AUDIENCE_STOP_CONFIDENCE
        and metrics.get("product_audience_gender") is not None
        and metrics.get("product_audience_gender_confidence", 0) >= AUDIENCE_STOP_CONFIDENCE
        and (metrics.get("target_audience_min_age") is not None or metrics.get("target_audience_max_age") is not None)
        and metrics.get("target_audience_age_confidence", 0) >= AUDIENCE_STOP_CONFIDENCE
        and (metrics.get("product_audience_min_age") is not None or metrics.get("product_audience_max_age") is not None)
        and metrics.get("product_audience_age_confidence", 0) >= AUDIENCE_STOP_CONFIDENCE
    )


def _ensure_latest_product_columns(db) -> None:
    db.execute(text("""
        ALTER TABLE brands_raw
            ADD COLUMN IF NOT EXISTS latest_product TEXT,
            ADD COLUMN IF NOT EXISTS latest_product_confidence INTEGER
    """))
    db.commit()


def _audience_prompt(
    brand,
    bio: str,
    business_category: str,
    posts: list[dict],
    page_history: list[str],
    re_creators: list[dict],
    current_page_url: str = "",
    current_page_text: str = "",
    final: bool = False,
) -> str:
    history = "\n\n".join(page_history) if page_history else "No earlier website pages have been analyzed."
    if final:
        crawl_instruction = (
            "FINAL PASS: all available website pages have been reviewed. Make your final best estimate from the complete evidence below. "
            "There are no more pages to fetch.\n\n"
        )
        website_text = history
    else:
        crawl_instruction = (
            "PAGE-BY-PAGE REVIEW: preserve useful findings from earlier pages and update them using the current page. "
            "The supplied website text lists earlier pages first and the current page last.\n\n"
        )
        website_text = (
            f"EARLIER PAGES:\n{history}\n\n"
            f"CURRENT PAGE ({current_page_url}):\n{current_page_text}"
        )

    template = AUDIENCE_PROMPT.replace(
        "You are analyzing a brand to determine its audience demographics.",
        crawl_instruction + "You are analyzing a brand to determine its audience demographics.",
    )
    return fill_template(
        template,
        brand_name=brand["brand_name"] or "Unknown",
        website=brand["website"],
        description=brand["description"],
        website_text=website_text,
        bio=bio,
        business_category_name=business_category,
        posts=json.dumps(posts, ensure_ascii=True, default=str),
        re_creators=json.dumps(re_creators, ensure_ascii=True) if re_creators else "None",
        today=datetime.now(timezone.utc).date().isoformat(),
    )


def _reverse_engineering_creators(db, brand_raw_id: int) -> list[dict]:
    """
    Distinct creators linked to the brand through reverse-engineering
    partnership posts (test_creator_brand_partnership_posts) with
    sponsorship_confidence >= 90.
    """
    rows = (
        db.query(
            TestCreatorBrandPartnershipPost.creator_username,
            TestCreatorBrandPartnershipPost.creator_name,
        )
        .filter(
            TestCreatorBrandPartnershipPost.brand_raw_id == brand_raw_id,
            TestCreatorBrandPartnershipPost.sponsorship_confidence >= 90,
        )
        .all()
    )
    creators: dict[str, dict] = {}
    for username, name in rows:
        if not username or not username.strip():
            continue
        key = username.strip().lower()
        if key not in creators or (name and not creators[key]["creator_name"]):
            creators[key] = {"creator_username": username.strip(), "creator_name": name}
    return list(creators.values())


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify brand audiences from brand websites.")
    parser.add_argument("--dry-run", action="store_true", help="List matching brands without scraping or calling the LLM.")
    parser.add_argument("--limit", type=int, help="Maximum number of qualifying brands to process.")
    parser.add_argument(
        "--brand-id",
        type=int,
        action="append",
        dest="brand_ids",
        help="Process one brand_raw ID. Repeat this option to process multiple IDs.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="With --brand-id: re-analyse the brand even if it already has audience results (overwrites them).",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.force and not args.brand_ids:
        parser.error("--force requires --brand-id (it re-analyses brands that already have results)")

    db = SessionLocal()
    try:
        _ensure_latest_product_columns(db)
        brands = db.execute(FORCE_BRAND_QUERY if args.force else BRAND_QUERY).mappings().all()
        if args.brand_ids:
            requested_ids = set(args.brand_ids)
            brands = [brand for brand in brands if brand["brand_raw_id"] in requested_ids]
            missing_ids = requested_ids - {brand["brand_raw_id"] for brand in brands}
            if missing_ids:
                logger.warning(
                    "Requested brand_raw_id(s) did not match the script's candidate conditions: %s",
                    sorted(missing_ids),
                )
        if not brands:
            logger.info("The brand query returned no rows.")
            return 0

        brand_ids = [brand["brand_raw_id"] for brand in brands]
        posts = (
            db.query(InstagramPost)
            .filter(InstagramPost.brand_raw_id.in_(brand_ids))
            .order_by(InstagramPost.brand_raw_id, InstagramPost.timestamp.desc().nullslast())
            .all()
        )
        posts_by_brand = defaultdict(list)
        for post in posts:
            posts_by_brand[post.brand_raw_id].append(post)

        eligible = [
            (brand, posts_by_brand[brand["brand_raw_id"]])
            for brand in brands
        ]
        if args.limit is not None:
            eligible = eligible[:args.limit]
        logger.info(
            "%d candidate brand(s); %d brand(s) selected for processing.",
            len(brands), len(eligible),
        )

        for brand, brand_posts in eligible:
            if args.dry_run:
                logger.info(
                    "DRY RUN brand_raw_id=%s brand=%s instagram_posts=%d",
                    brand["brand_raw_id"], brand["brand_name"], len(brand_posts),
                )
                continue

            bio = next((post.biography for post in brand_posts if post.biography), "Not available")
            business_category = next(
                (post.business_category_name for post in brand_posts if post.business_category_name),
                "Not available",
            )
            evidence = [
                {
                    "timestamp": post.timestamp,
                    "caption": post.caption,
                    "hashtags": post.hashtags,
                    "coauthor_producers": post.coauthor_producers or [],
                    "mentions": post.mentions or [],
                    "sponsors": post.sponsors or [],
                    "tagged_users": post.tagged_users or [],
                }
                for post in brand_posts[:MAX_INSTAGRAM_POSTS_FOR_LLM]
            ]
            re_creators = _reverse_engineering_creators(db, brand["brand_raw_id"])
            origin = _normalize_origin(brand["website"])
            queue = deque([origin])
            queued = {_url_key(origin)}
            visited: set[str] = set()
            page_history: list[str] = []
            pages_log: list[dict] = []
            metrics: dict | None = None
            stopped_early = False

            while queue and len(pages_log) < MAX_WEBSITE_PAGES:
                page_url = queue.popleft()
                page_key = _url_key(page_url)
                if page_key in visited:
                    continue
                visited.add(page_key)

                html = _fetch_page(page_url)
                if html is None:
                    pages_log.append({"url": page_url, "status": "fetch_failed"})
                    logger.warning("Website page fetch failed for brand_raw_id=%s url=%s", brand["brand_raw_id"], page_url)
                    continue

                for link in _discover_audience_links(html, origin):
                    link_key = _url_key(link)
                    if link_key not in visited and link_key not in queued:
                        queue.append(link)
                        queued.add(link_key)

                page_text = _clean_page_text(html)
                if len(page_text) < 40:
                    pages_log.append({"url": page_url, "status": "no_text"})
                    logger.info("No useful text on brand_raw_id=%s page=%s", brand["brand_raw_id"], page_url)
                    continue

                result = call_gpt_json(
                    _audience_prompt(
                        brand,
                        bio,
                        business_category,
                        evidence,
                        page_history,
                        re_creators,
                        current_page_url=page_url,
                        current_page_text=page_text,
                    ),
                    context=f"brand website audience page brand_raw_id={brand['brand_raw_id']} url={page_url}",
                    model=LLM_MODEL,
                )
                page_metrics = _metrics(result)
                metrics = _merge_metrics(metrics, page_metrics)
                page_history.append(f"PAGE URL: {page_url}\n{page_text}")
                pages_log.append({
                    "url": page_url,
                    "status": "ok",
                    "target_gender": metrics["target_audience_gender"],
                    "target_gender_confidence": metrics["target_audience_gender_confidence"],
                    "target_age": [metrics["target_audience_min_age"], metrics["target_audience_max_age"]],
                    "target_age_confidence": metrics["target_audience_age_confidence"],
                    "product_gender": metrics["product_audience_gender"],
                    "product_gender_confidence": metrics["product_audience_gender_confidence"],
                    "product_age": [metrics["product_audience_min_age"], metrics["product_audience_max_age"]],
                    "product_age_confidence": metrics["product_audience_age_confidence"],
                })
                logger.info(
                    "Audience page reviewed brand_raw_id=%s pages=%d/%d url=%s target_gender=%s product_gender=%s",
                    brand["brand_raw_id"], len(pages_log), MAX_WEBSITE_PAGES, page_url,
                    metrics["target_audience_gender"], metrics["product_audience_gender"],
                )
                if _has_confident_estimates(metrics):
                    stopped_early = True
                    logger.info(
                        "Stopping website crawl for brand_raw_id=%s: all four audience dimensions have estimates with confidence >= %d.",
                        brand["brand_raw_id"], AUDIENCE_STOP_CONFIDENCE,
                    )
                    break

            if not page_history:
                logger.warning("No readable website text for brand_raw_id=%s; skipping.", brand["brand_raw_id"])
                continue

            # Like geo_reach, run a final synthesis when the confidence gate
            # did not end the crawl early. All scraped page text is retained.
            if not stopped_early:
                final_result = call_gpt_json(
                    _audience_prompt(
                        brand,
                        bio,
                        business_category,
                        evidence,
                        page_history,
                        re_creators,
                        final=True,
                    ),
                    context=f"brand website audience final brand_raw_id={brand['brand_raw_id']}",
                    model=LLM_MODEL,
                )
                final_metrics = _metrics(final_result)
                metrics = _merge_metrics(metrics, final_metrics)
                # The final pass has seen every page, so it decides the latest
                # product — including clearing it to null when no product is
                # both recently launched and actively marketed.
                if final_result:
                    metrics["latest_product"] = final_metrics["latest_product"]
                    metrics["latest_product_confidence"] = final_metrics["latest_product_confidence"]

            if metrics is None:
                logger.warning("No audience estimates returned for brand_raw_id=%s; skipping save.", brand["brand_raw_id"])
                continue

            updated = (
                db.query(BrandRaw)
                .filter(BrandRaw.id == brand["brand_raw_id"])
                .update(metrics, synchronize_session=False)
            )
            db.commit()
            if not updated:
                logger.error("Could not save audience metrics for brand_raw_id=%s", brand["brand_raw_id"])
            print(json.dumps({
                "brand_raw_id": brand["brand_raw_id"],
                "brand_name": brand["brand_name"],
                "post_count": len(brand_posts),
                "website_pages_scraped": pages_log,
                "stopped_early": stopped_early,
                **metrics,
            }, ensure_ascii=True, default=str))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
