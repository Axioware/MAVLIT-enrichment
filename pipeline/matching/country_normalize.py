"""
pipeline/matching/country_normalize.py

Country names come from different sources with different conventions —
creator-submitted audience_top_countries uses whatever a creator typed
(often a 2-letter code like "US"), while brand country/operating_area
comes from Wikidata scraping and tends toward full/colloquial names
("America", "pakistan", "China" — confirmed against real data). Both need
to resolve to the same canonical form before any overlap check, or
semantically identical countries (US == America) silently fail to match.

Not a full ISO-3166 implementation — a pragmatic alias table covering
common variants, with substring matching as a fallback for anything
unmapped. "worldwide" as an operating_area is treated as a special case
that matches any country (handled by the caller, not here).
"""

_ALIASES: dict[str, str] = {
    "us": "united states", "usa": "united states", "u.s.": "united states",
    "u.s.a.": "united states", "united states of america": "united states",
    "america": "united states", "united states": "united states",
    "uk": "united kingdom", "u.k.": "united kingdom", "britain": "united kingdom",
    "great britain": "united kingdom", "england": "united kingdom",
    "united kingdom": "united kingdom",
    "uae": "united arab emirates", "united arab emirates": "united arab emirates",
    "south korea": "south korea", "korea": "south korea",
}


def normalize_country(name: str | None) -> str | None:
    """Returns a canonical lowercase country name, or the lowercased input if unmapped."""
    if not name:
        return None
    key = name.strip().lower()
    return _ALIASES.get(key, key)


def countries_match(a: str | None, b: str | None) -> bool:
    """
    True if two country strings refer to the same country. Falls back to
    substring containment (after normalization) so e.g. a broader
    brand.operating_area string mentioning a country name still matches.
    """
    na, nb = normalize_country(a), normalize_country(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na
