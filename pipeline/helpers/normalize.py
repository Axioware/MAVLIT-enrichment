import re
from rapidfuzz import fuzz, process

# Legal entity suffixes to strip from brand names
_SUFFIX_PATTERN = re.compile(
    r",?\s*(Inc\.?|LLC\.?|Ltd\.?|Corp\.?|Co\.|GmbH|S\.A\.)$",
    flags=re.IGNORECASE,
)

FUZZY_THRESHOLD = 88


def normalize(name: str) -> str:
    """Lowercase, strip legal suffixes, collapse whitespace."""
    name = name.strip().lower()
    name = _SUFFIX_PATTERN.sub("", name)
    return " ".join(name.split())


def deduplicate(names: list[str], threshold: int = FUZZY_THRESHOLD) -> list[str]:
    """
    Remove near-duplicates using token_sort_ratio fuzzy matching.
    Runs in-memory on a single batch — DB UNIQUE constraint is the final guard.

    Watch for false positives: 'zara' vs 'zara home' are distinct brands
    and should NOT merge at threshold=88.
    """
    seen: list[str] = []
    for name in names:
        if not name:
            continue
        match = process.extractOne(name, seen, scorer=fuzz.token_sort_ratio)
        if match is None or match[1] < threshold:
            seen.append(name)
    return seen