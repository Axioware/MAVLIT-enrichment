"""The content mix an LLM estimates for a creator from their captions: the share of product-focused posts and the five biggest content
categories, in percent. Used for MAVLIT users (pipeline/creator_content/instagram_description.py) and for the creators of the Feed
(pipeline/enrichment/creator_niches.py)."""
from pipeline.helpers.prompts import CREATOR_CONTENT_CATEGORIES

CONTENT_CATEGORIES_SHOWN = 5


def _percent(value) -> int | None:
    """A whole percentage from 0 to 100, or None (a number or a numeric string, "32%" included)."""
    if isinstance(value, str):
        value = value.strip().rstrip("%").strip()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, bool) or number != number or not 0 <= number <= 100:
        return None
    return int(round(number))


def content_mix_from_result(result: dict) -> dict:
    """
    The content mix part of the description LLM's JSON: {"product_focused_pct": int|None, "content_categories": [{"name", "pct"}, ...]|None}.
    The categories are only the allowed names (matched ignoring case), one entry each, the five biggest first; their percentages are whole
    numbers and are scaled down when the LLM's add up to more than 100 (a total below 100 is kept: the rest is not in the top five or not
    sorted). Missing or unusable values become None - e.g. when the prompt copy in /admin predates the content mix.
    """
    allowed = {c.lower(): c for c in CREATOR_CONTENT_CATEGORIES}
    raw = result.get("content_categories")
    pairs = raw.items() if isinstance(raw, dict) else ((i.get("name"), i.get("pct")) for i in raw if isinstance(i, dict)) if isinstance(raw, list) else ()
    shares: dict[str, float] = {}
    for name, value in pairs:
        pct = _percent(value)
        category = allowed.get(name.strip().lower()) if isinstance(name, str) else None
        if category and pct:
            shares[category] = shares.get(category, 0) + pct
    total = sum(shares.values())
    if total > 100:
        shares = {k: v * 100 / total for k, v in shares.items()}
    ranked = sorted(shares.items(), key=lambda kv: (-kv[1], kv[0]))[:CONTENT_CATEGORIES_SHOWN]
    categories = [{"name": name, "pct": int(round(pct))} for name, pct in ranked if int(round(pct)) > 0] or None
    return {"product_focused_pct": _percent(result.get("product_focused_pct")), "content_categories": categories}
