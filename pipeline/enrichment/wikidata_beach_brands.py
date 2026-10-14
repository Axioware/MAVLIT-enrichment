"""
pipeline/enrichment/wikidata_beach_brands.py

Discover brands with a genuine beach / water-sports / outdoor-athlete
connection from Wikidata, and print them in the terminal. Discovery only —
nothing is written to the database.

Categories (--category, default all; see CATEGORY_LABELS): surfing, swimming,
water_sports, beach_sports, sun_protection, beach_activewear, beach_footwear,
diving_snorkeling, paddle_sports, beach_fitness, beach_recovery, hydration,
sports_nutrition, beach_haircare, beach_skincare, beach_accessories,
beach_cooling, marine_safety, beach_camping, beach_fishing, beach_eyewear,
beach_travel, marine_technology, ocean_conservation.

Run from the project root:
    python -m pipeline.enrichment.wikidata_beach_brands
    python -m pipeline.enrichment.wikidata_beach_brands --limit 10
    python -m pipeline.enrichment.wikidata_beach_brands --limit 10 --category surfing
    python -m pipeline.enrichment.wikidata_beach_brands --limit 10 --category sun_protection
    python -m pipeline.enrichment.wikidata_beach_brands --limit 20 --min-relevance HIGH --output beach_brands.json

Discovery strategies (all live Wikidata queries, merged and deduplicated):
  A. Product relationships — beach product concepts (surfboard, wetsuit,
     swimsuit, ...) are resolved by exact English label at run time (only
     items that are real classes, i.e. have a P279 statement), expanded to
     their subclasses for the core product types, then:
       - company -> product:  ?company wdt:P1056 ?product
       - product -> maker:    ?item wdt:P31 ?product . ?item wdt:P176 ?company
  B. Industry relationships — companies with P452 in a resolved industry
     ("swimwear industry", "sporting goods industry") or P31 "sporting goods
     manufacturer". These are broad on purpose; a candidate found only this
     way needs beach evidence to pass classification.
  C. Keyword search — Wikidata full-text search for beach terms restricted to
     items that are businesses / companies / brands (haswbstatement:P31=...).
  D. Merge — candidates are deduplicated by QID, details for all of them are
     fetched in batched SPARQL, then deduplicated again by website domain.

Relevance (rule-based, no LLM): see classify_relevance(). A word like "beach"
in a description is never enough on its own; evidence must name a beach /
water-sports product or activity, and negative contexts (surf schools, pool
supplies, software, hotels, ...) disqualify a candidate.

Website comes from Wikidata P856 and Instagram from Wikidata P2003 — both are
reported as "from Wikidata, not independently verified".

Market coverage (see classify_coverage()): by default only brands that
operate in the US are shown — Wikidata evidence of operating WORLDWIDE, in
the UNITED STATES (incl. any US-based brand), or in MULTIPLE COUNTRIES
INCLUDING THE US. Pass --all-coverage to also
show brands whose coverage is unknown or limited to non-US countries.
"""

import argparse
import json
import logging
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from pipeline.sources.wikidata import _extract_domain, _website_score

logger = logging.getLogger(__name__)

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
USER_AGENT = (
    "MAVLIT-enrichment/1.0 (https://github.com/axioware/MAVLIT-enrichment; "
    "beach-brand discovery script)"
)

DEFAULT_TIMEOUT = 60.0     # seconds per request (project SPARQL calls use 30-120)
MAX_ATTEMPTS = 4           # per request, then give up
MAX_RETRY_AFTER = 120      # cap on a Retry-After header (same as pipeline/sources/wikidata.py)
BACKOFF_BASE = 2.0         # exponential backoff for transient errors: 2, 4, 8 s (+ jitter)
QUERY_PAUSE = 1.0          # politeness pause between Wikidata requests
DETAIL_BATCH = 40          # entities per details query
SUBCLASS_CAP = 150         # max product classes after subclass expansion
RESULT_CAP = 1500          # LIMIT on relationship queries (many product classes now)

EXIT_OK = 0
EXIT_WIKIDATA_UNAVAILABLE = 3

CATEGORIES = (
    "surfing", "swimming", "water_sports", "beach_sports", "sun_protection", "beach_activewear",
    "beach_footwear", "diving_snorkeling", "paddle_sports", "beach_fitness", "beach_recovery",
    "hydration", "sports_nutrition", "beach_haircare", "beach_skincare", "beach_accessories",
    "beach_cooling", "marine_safety", "beach_camping", "beach_fishing", "beach_eyewear",
    "beach_travel", "marine_technology", "ocean_conservation",
)
CATEGORY_LABELS = {
    "surfing": "Surfing and surf apparel",
    "swimming": "Swimming and swimwear",
    "water_sports": "Water sports equipment",
    "beach_sports": "Beach sports",
    "sun_protection": "Sun protection and outdoor skincare",
    "beach_activewear": "Beach and water-sports apparel",
    "beach_footwear": "Beach and water-sports footwear",
    "diving_snorkeling": "Diving and snorkeling equipment",
    "paddle_sports": "Paddleboarding and paddling equipment",
    "beach_fitness": "Beach workout and outdoor fitness equipment",
    "beach_recovery": "Sports recovery for outdoor athletes",
    "hydration": "Hydration and sports nutrition",
    "sports_nutrition": "Sports nutrition for outdoor athletes",
    "beach_haircare": "Haircare for sun and saltwater exposure",
    "beach_skincare": "Skincare for beach environments",
    "beach_accessories": "Beach and outdoor accessories",
    "beach_cooling": "Cooling and heat-management products",
    "marine_safety": "Water-sports and ocean safety",
    "beach_camping": "Beach camping and outdoor recreation",
    "beach_fishing": "Fishing and coastal recreation",
    "beach_eyewear": "Sunglasses and eye protection",
    "beach_travel": "Beach travel equipment",
    "marine_technology": "Water-sports technology",
    "ocean_conservation": "Ocean-conscious consumer products",
}

# Strategy A: product concepts, resolved by exact English label at run time.
# (label, category, expand_to_subclasses). A label with no matching Wikidata
# class is simply skipped — keyword search (Strategy C) still covers it.
PRODUCT_CONCEPTS: tuple[tuple[str, str, bool], ...] = (
    ("surfboard", "surfing", True),
    ("bodyboard", "surfing", False),
    ("boardshorts", "surfing", False),
    ("Surfwear", "surfing", False),
    ("skimboard", "surfing", False),
    ("surf wax", "surfing", False),
    ("leash", "surfing", False),
    ("swimsuit", "swimming", True),
    ("competitive swimwear", "swimming", False),
    ("bikini", "swimming", False),
    ("swim goggles", "swimming", False),
    ("swim cap", "swimming", False),
    ("swimfins", "swimming", False),
    ("kickboard", "swimming", False),
    ("pull buoy", "swimming", False),
    ("swim briefs", "swimming", False),
    ("wetsuit", "water_sports", True),
    ("rash guard", "water_sports", False),
    ("windsurfing board", "water_sports", False),
    ("Kiteboard", "water_sports", False),
    ("wakeboard", "water_sports", False),
    ("stand-up paddleboard", "water_sports", False),
    ("paddleboard", "water_sports", False),
    ("snorkel", "water_sports", False),
    ("dry suit", "water_sports", False),
    ("water ski", "water_sports", False),
    ("beach volleyball", "beach_sports", False),
    ("volleyball", "beach_sports", False),
    ("flying disc", "beach_sports", False),
    ("sunscreen", "sun_protection", True),
    ("sun protective clothing", "sun_protection", False),
    ("flip-flops", "beach_footwear", False),
    ("sandal", "beach_footwear", False),
    ("water shoe", "beach_footwear", False),
    ("diving mask", "diving_snorkeling", False),
    ("diving regulator", "diving_snorkeling", False),
    ("buoyancy compensator", "diving_snorkeling", False),
    ("dive computer", "diving_snorkeling", False),
    ("diving equipment", "diving_snorkeling", True),
    ("kayak", "paddle_sports", True),
    ("canoe", "paddle_sports", False),
    ("paddle", "paddle_sports", False),
    ("resistance band", "beach_fitness", False),
    ("pull-up bar", "beach_fitness", False),
    ("skipping rope", "beach_fitness", False),
    ("foam roller", "beach_recovery", False),
    ("massage gun", "beach_recovery", False),
    ("sports drink", "hydration", True),
    ("oral rehydration therapy", "hydration", False),
    ("hydration pack", "hydration", False),
    ("water bottle", "hydration", False),
    ("protein bar", "sports_nutrition", False),
    ("energy gel", "sports_nutrition", False),
    ("protein supplement", "sports_nutrition", False),
    ("dietary supplement", "sports_nutrition", False),
    ("beach towel", "beach_accessories", False),
    ("beach umbrella", "beach_accessories", False),
    ("dry bag", "beach_accessories", False),
    ("ice pack", "beach_cooling", False),
    ("personal flotation device", "marine_safety", True),
    ("lifebuoy", "marine_safety", False),
    ("emergency position-indicating radiobeacon", "marine_safety", False),
    ("hammock", "beach_camping", False),
    ("tent", "beach_camping", False),
    ("sleeping bag", "beach_camping", False),
    ("fishing rod", "beach_fishing", False),
    ("fishing reel", "beach_fishing", False),
    ("fishing tackle", "beach_fishing", False),
    ("fishing lure", "beach_fishing", False),
    ("sunglasses", "beach_eyewear", True),
    ("action camera", "marine_technology", False),
    ("underwater camera", "marine_technology", False),
    ("smartwatch", "marine_technology", False),
)

# Strategy B: industries (P452 values) and company classes (P31 values).
INDUSTRY_CONCEPTS: tuple[str, ...] = ("swimwear industry", "sporting goods industry")
COMPANY_CLASS_CONCEPTS: tuple[str, ...] = ("sporting goods manufacturer",)

# Strategy C: keyword searches (term, category).
KEYWORD_TERMS: tuple[tuple[str, str], ...] = (
    ("surfwear", "surfing"),
    ("surfboard", "surfing"),
    ("surfing apparel", "surfing"),
    ("bodyboard", "surfing"),
    ("skimboard", "surfing"),
    ("surf wax", "surfing"),
    ("surf fins", "surfing"),
    ("surf poncho", "surfing"),
    ("swimwear", "swimming"),
    ("swimsuit", "swimming"),
    ("competitive swimwear", "swimming"),
    ("swimming equipment", "swimming"),
    ("swim goggles", "swimming"),
    ("swim parka", "swimming"),
    ("wetsuit", "water_sports"),
    ("water sports equipment", "water_sports"),
    ("paddleboard", "water_sports"),
    ("windsurfing", "water_sports"),
    ("kitesurfing", "water_sports"),
    ("wakeboard", "water_sports"),
    ("drysuit", "water_sports"),
    ("water ski", "water_sports"),
    ("beach volleyball", "beach_sports"),
    ("beach tennis", "beach_sports"),
    ("beach soccer", "beach_sports"),
    ("spikeball", "beach_sports"),
    ("paddleball", "beach_sports"),
    ("sunscreen", "sun_protection"),
    ("mineral sunscreen", "sun_protection"),
    ("sun care", "sun_protection"),
    ("after-sun", "sun_protection"),
    ("UV protection clothing", "sun_protection"),
    ("swim leggings", "beach_activewear"),
    ("rash vest", "beach_activewear"),
    ("UV swimwear", "beach_activewear"),
    ("swim trunks", "beach_activewear"),
    ("water shoes", "beach_footwear"),
    ("flip-flops", "beach_footwear"),
    ("beach sandals", "beach_footwear"),
    ("surf booties", "beach_footwear"),
    ("scuba diving equipment", "diving_snorkeling"),
    ("snorkel", "diving_snorkeling"),
    ("dive computer", "diving_snorkeling"),
    ("spearfishing", "diving_snorkeling"),
    ("stand-up paddleboard", "paddle_sports"),
    ("kayak", "paddle_sports"),
    ("canoe paddle", "paddle_sports"),
    ("resistance bands", "beach_fitness"),
    ("suspension trainer", "beach_fitness"),
    ("outdoor fitness equipment", "beach_fitness"),
    ("massage gun", "beach_recovery"),
    ("foam roller", "beach_recovery"),
    ("compression boots", "beach_recovery"),
    ("sports recovery", "beach_recovery"),
    ("electrolyte drink", "hydration"),
    ("sports drink", "hydration"),
    ("hydration pack", "hydration"),
    ("insulated water bottle", "hydration"),
    ("protein powder", "sports_nutrition"),
    ("protein bar", "sports_nutrition"),
    ("energy gel", "sports_nutrition"),
    ("sports nutrition", "sports_nutrition"),
    ("hair sun protection", "beach_haircare"),
    ("swimmer shampoo", "beach_haircare"),
    ("aloe vera gel", "beach_skincare"),
    ("after sun lotion", "beach_skincare"),
    ("beach towel", "beach_accessories"),
    ("beach umbrella", "beach_accessories"),
    ("beach chair", "beach_accessories"),
    ("dry bag", "beach_accessories"),
    ("cooling towel", "beach_cooling"),
    ("portable cooler", "beach_cooling"),
    ("portable fan", "beach_cooling"),
    ("life jacket", "marine_safety"),
    ("personal flotation device", "marine_safety"),
    ("rescue buoy", "marine_safety"),
    ("camping hammock", "beach_camping"),
    ("tent", "beach_camping"),
    ("camping equipment", "beach_camping"),
    ("fishing tackle", "beach_fishing"),
    ("fishing rod", "beach_fishing"),
    ("fishing reel", "beach_fishing"),
    ("sunglasses", "beach_eyewear"),
    ("polarized sunglasses", "beach_eyewear"),
    ("sports eyewear", "beach_eyewear"),
    ("waterproof backpack", "beach_travel"),
    ("travel towel", "beach_travel"),
    ("action camera", "marine_technology"),
    ("underwater camera", "marine_technology"),
    ("GPS sports watch", "marine_technology"),
    ("waterproof headphones", "marine_technology"),
    ("ocean plastic", "ocean_conservation"),
    ("reef-safe", "ocean_conservation"),
    ("sustainable swimwear", "ocean_conservation"),
    ("reusable water bottle", "ocean_conservation"),
)

# Entities must be (subclasses of) business, brand or trademark.
BUSINESS_TYPE_QIDS = ("Q4830453", "Q431289", "Q167270")
# Restricts keyword search to items whose P31 is a common company/brand class.
KEYWORD_P31_FILTER = "haswbstatement:" + "|".join(
    f"P31={q}" for q in ("Q4830453", "Q6881511", "Q891723", "Q167037", "Q431289",
                         "Q783794", "Q24928712", "Q658255", "Q167270")
)

# Relevance vocabulary — word-boundary regexes per category. Generic words
# are only matched in a beach/sport phrase ("electrolyte powder", not
# "electrolyte" — battery makers; "beach slides", not "slides"; "fishing
# tackle", not "tackle"; "beach towel", not "towel").
RELEVANCE_TERMS: dict[str, tuple[str, ...]] = {
    "surfing": (
        r"surf(?:ing|wear|board|boards|er|ers|\s+apparel|\s+brand|\s+clothing|\s+equipment|\s+company)?",
        r"board\s?shorts?", r"body\s?boards?|bodyboarding",
        r"skim\s?boards?|skimboarding", r"(?:longboard|shortboard)\s+surfboards?",
        r"surf\s+(?:wax|leash\w*|fins?|traction(?:\s+pads?)?|ponchos?)", r"traction\s+pads?",
    ),
    "swimming": (
        r"swim(?:wear|suits?|ming|\s+wear|\s+goggles|\s+caps?|\s+fins|fins|\s+trunks|\s+briefs|\s+paddles|\s+parkas?)?",
        r"bikinis?", r"tankinis?", r"bathing\s+suits?",
        r"kick\s?boards?", r"pull\s+buoys?", r"open[- ]water\s+(?:swimming\s+)?buoys?",
    ),
    "water_sports": (
        r"wetsuits?|wet\s+suits?", r"water\s?sports?", r"aquatic\s+sports?",
        r"paddle\s?boards?|paddleboarding|stand[- ]up\s+paddle\w*",
        r"windsurf\w*", r"kite\s?surf\w*|kite\s?boards?|kiteboarding", r"wake\s?boards?\w*",
        r"rash\s?guards?", r"snorkel\w*", r"dry\s?suits?", r"water[- ]?ski\w*", r"towables?|towable\s+tubes?",
    ),
    "beach_sports": (
        r"beach\s+(?:volleyball|tennis|soccer|football|badminton|handball|paddleball|cricket|frisbee|sports?)",
        r"spikeball|roundnet", r"paddleball", r"flying\s+discs?|frisbees?",
    ),
    "sun_protection": (
        r"sunscreens?|sun\s?screens?|sun\s?blocks?|sun\s?care|sun\s+protection|spf|after[- ]sun",
        r"sunburn\w*|uv[- ]protect\w*|upf\s?\d*|zinc\s+oxide|reef[- ](?:safe|friendly|conscious)\s+sunscreens?",
    ),
    "beach_activewear": (
        r"swim\s+(?:leggings|shirts?|trunks)|swim\s?trunks", r"rash\s+vests?", r"surf\s+leggings",
        r"uv\s+swimwear", r"water[- ]?sports?\s+(?:clothing|apparel)", r"quick[- ]dry\s+(?:shorts|clothing|apparel)",
        r"beach\s+activewear",
    ),
    "beach_footwear": (
        r"water\s+shoes|aqua\s+shoes|reef\s+shoes", r"surf\s+boot(?:ie)?s|neoprene\s+boots|div(?:e|ing)\s+boots",
        r"sandals?|flip[- ]?flops?|thongs\s+\(footwear\)", r"beach\s+slides|pool\s+slides",
    ),
    "diving_snorkeling": (
        r"scuba|recreational\s+diving|dive\s+gear|diving\s+(?:equipment|gear|masks?|fins|regulators?|computers?)",
        r"dive\s+(?:computers?|masks?|equipment)", r"snorkel\w*", r"buoyancy\s+compensators?",
        r"spearfishing", r"underwater\s+(?:camera\s+)?housings?",
    ),
    "paddle_sports": (
        r"sup\s+boards?|stand[- ]up\s+paddle\w*|paddle\s?boards?|paddleboarding|inflatable\s+paddleboards?",
        r"kayaks?|kayaking|canoes?|canoeing", r"(?:kayak|canoe)\s+paddles?", r"paddleboard\s+(?:pumps?|leash\w*|accessories)",
    ),
    "beach_fitness": (
        r"resistance\s+bands?", r"(?:portable\s+)?pull[- ]up\s+bars?", r"suspension\s+train\w*",
        r"agility\s+ladders?", r"speed\s+ropes?|jump\s+ropes?|skipping\s+ropes?", r"sandbag\s+training|training\s+sandbags?",
        r"(?:portable|outdoor|beach)\s+(?:workout|fitness)\s+equipment|outdoor\s+fitness|beach\s+workouts?",
    ),
    "beach_recovery": (
        r"massage\s+guns?|percussion\s+massag\w*", r"foam\s+rollers?", r"compression\s+boots",
        r"(?:muscle|sports?)\s+recovery|recovery\s+(?:balms?|drinks?|products|tools?|boots)", r"cooling\s+towels?",
    ),
    "hydration": (
        r"electrolyte\s+(?:powders?|tablets?|drinks?|drink\s+mix(?:es)?|supplements?|replacement)",
        r"hydration\s+(?:drinks?|packs?|vests?|multipliers?|powders?|supplements?|products|bladders?)",
        r"sports?\s+drinks?", r"oral\s+rehydration", r"insulated\s+water\s+bottles?",
    ),
    "sports_nutrition": (
        r"protein\s+(?:powders?|bars?|shakes?|supplements?)", r"energy\s+gels?", r"endurance\s+nutrition",
        r"carbohydrate\s+drink\s+mix(?:es)?", r"sports?\s+(?:nutrition|supplements?)", r"hydration\s+supplements?",
    ),
    "beach_haircare": (
        r"saltwater\s+hair\w*", r"uv\s+hair\s+protection|hair\s+(?:sun\s?protection|sunscreen)",
        r"after[- ]swim\s+(?:shampoo|hair\s?care)|swimmers?'?\s+shampoo|chlorine\s+remov\w*\s+shampoo",
        r"ocean\s+hair\s?care", r"leave[- ]in\s+conditioners?", r"sun\s+protection\s+hair\s+sprays?",
    ),
    "beach_skincare": (
        r"after[- ]sun|aloe\s+vera(?:\s+gel)?", r"sunburn\s+(?:care|relief)", r"saltwater\s+skin\s?care",
        r"outdoor\s+skin\s?care", r"water[- ]resistant\s+skin\s?care", r"moisturi[sz]ing\s+lotions?",
    ),
    "beach_accessories": (
        r"beach\s+(?:towels?|umbrellas?|tents?|canop(?:y|ies)|chairs?|blankets?|bags?|gear|accessories|carts?|wagons?)",
        r"quick[- ]dry\s+towels?", r"dry\s+bags?", r"waterproof\s+(?:bags?|phone\s+(?:pouch(?:es)?|cases?))",
    ),
    "beach_cooling": (
        r"cooling\s+(?:towels?|vests?)", r"portable\s+fans?", r"(?:insulated|soft|hard|portable|camping)\s+coolers?",
        r"ice\s+(?:packs?|chests?)", r"insulated\s+(?:beach\s+)?bags?", r"(?:portable\s+)?shade\s+canop(?:y|ies)",
    ),
    "marine_safety": (
        r"life\s+(?:jackets?|vests?)|lifejackets?|personal\s+flotation\s+devices?",
        r"(?:swim|rescue)\s+buoys?|lifebuoys?", r"marine\s+safety|water[- ]safety\s+equipment",
        r"waterproof\s+first\s+aid", r"emergency\s+(?:position[- ]indicating\s+)?(?:marine\s+)?(?:radio\s?)?beacons?",
    ),
    "beach_camping": (
        r"camping\s+(?:hammocks?|coolers?|gear|equipment|tents?)|hammocks?", r"tents?", r"sleeping\s+bags?",
        r"portable\s+(?:showers?|shelters?)", r"outdoor\s+mats?", r"outdoor\s+recreation\s+(?:equipment|gear|products)",
    ),
    "beach_fishing": (
        r"fishing\s+(?:rods?|reels?|tackle|gear|equipment|waders|lures?|lines?|sunglasses|tackle\s+bags?)",
        r"surf\s+fishing|saltwater\s+fishing|sport\s?fishing|angling", r"spinning\s+reels?", r"tackle\s+(?:bags?|boxes?)",
    ),
    "beach_eyewear": (
        r"sunglasses|polari[sz]ed\s+(?:lenses|eyewear)", r"sports?\s+eyewear|water[- ]?sports?\s+eyewear",
        r"swim(?:ming)?\s+goggles", r"uv\s+protection\s+(?:sunglasses|eyewear)",
    ),
    "beach_travel": (
        r"beach\s+travel\s+bags?", r"waterproof\s+(?:luggage|backpacks?)", r"travel\s+towels?",
        r"water[- ]resistant\s+(?:backpacks?|bags?|luggage)", r"snorkel(?:ing)?\s+travel\s+gear",
    ),
    "marine_technology": (
        r"action\s+cameras?|underwater\s+cameras?", r"waterproof\s+(?:cameras?|smartwatch\w*|watches|headphones|earbuds|speakers?)",
        r"swim(?:ming)?\s+(?:tracking|headphones|watch\w*)", r"gps\s+(?:sports?\s+)?watch\w*", r"dive\s+computers?",
    ),
    "ocean_conservation": (
        r"ocean[- ](?:friendly|conscious|plastic|cleanup|clean-up)", r"plastic[- ]free", r"reusable\s+(?:water\s+)?bottles?",
        r"sustainable\s+swimwear", r"recycled\s+(?:ocean\s+)?plastics?", r"reef[- ](?:safe|friendly|conscious)",
    ),
}
# Kept for the MEDIUM cap mechanism; empty now that diving/snorkeling is its
# own requested category instead of "aquatic but not beach fitness".
WEAK_TERMS: tuple[str, ...] = ()
NEGATIVE_CONTEXT = re.compile(
    r"\b(?:schools?|camps?|lessons?|rentals?|tour\s+operators?|governing\s+body|leagues?|federations?|"
    r"associations?|hotels?|resorts?|spas?|museums?|festivals?|films?|album|magazines?|television|restaurants?|"
    r"software|web\s+design|agency|detergent|dairy|chemical|paint|swimming\s+pool|pool\s+(?:supplies|"
    r"equipment|systems|products)|wikimedia|disambiguation|human|family\s+name|given\s+name|"
    r"census|village|city|town|lake|beach\s+in|island|parish)\b",
    re.I,
)
DEFUNCT = re.compile(r"\b(?:former|defunct|dissolved|bankrupt|closed\s+down)\b", re.I)
BUSINESS_NOUN = re.compile(
    r"\b(?:company|companies|brand|manufactur\w*|maker|producer|label|apparel|clothing|wear|equipment|"
    r"retailer|store|shop|line|designer|business|corporation|firm|enterprise|goods|products?)\b",
    re.I,
)

_COMPILED = {cat: [re.compile(rf"\b(?:{t})\b", re.I) for t in terms] for cat, terms in RELEVANCE_TERMS.items()}
_WEAK = [re.compile(rf"\b(?:{t})\b", re.I) for t in WEAK_TERMS]
_INSTAGRAM_HANDLE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_BLOCKED_WEBSITE_HOSTS = ("wikipedia.org", "wikidata.org", "wikimedia.org", "google.", "bing.com",
                          "duckduckgo.com", "facebook.com", "instagram.com")
_QID = re.compile(r"^Q\d+$")
_SEP = "||"


class WikidataUnavailableError(RuntimeError):
    """Wikidata could not be reached (after bounded retries)."""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class WikidataClient:
    """
    Small Wikidata client: descriptive User-Agent, per-request timeout,
    honours 429 Retry-After (capped), bounded exponential backoff for
    timeouts / connection errors / 5xx, never retries forever.
    """

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, http: httpx.Client | None = None,
                 sleep=time.sleep, pause: float = QUERY_PAUSE):
        self._http = http or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout)
        self._timeout = timeout
        self._sleep = sleep
        self._pause = pause
        self._last_request = 0.0
        self.requests_made = 0

    def close(self) -> None:
        self._http.close()

    def _get(self, url: str, params: dict, accept: str, method: str = "GET") -> dict:
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            wait_for = self._pause - (time.monotonic() - self._last_request)
            if wait_for > 0:
                self._sleep(wait_for)
            self._last_request = time.monotonic()
            self.requests_made += 1
            try:
                headers = {"Accept": accept, "User-Agent": USER_AGENT}
                if method == "POST":   # long SPARQL queries exceed GET URL limits (HTTP 414)
                    resp = self._http.post(url, data=params, headers=headers, timeout=self._timeout)
                else:
                    resp = self._http.get(url, params=params, headers=headers, timeout=self._timeout)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                logger.warning("Wikidata request failed (%s), attempt %d/%d", type(exc).__name__,
                               attempt, MAX_ATTEMPTS)
            else:
                if resp.status_code == 429:
                    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                    last_error = WikidataUnavailableError("HTTP 429 Too Many Requests")
                    logger.warning("Wikidata rate limit (429) — waiting %ds, attempt %d/%d",
                                   retry_after, attempt, MAX_ATTEMPTS)
                    if attempt < MAX_ATTEMPTS:
                        self._sleep(retry_after)
                    continue
                if resp.status_code >= 500:
                    last_error = WikidataUnavailableError(f"HTTP {resp.status_code}")
                    logger.warning("Wikidata server error %d, attempt %d/%d", resp.status_code,
                                   attempt, MAX_ATTEMPTS)
                elif resp.status_code >= 400:
                    raise WikidataUnavailableError(f"Wikidata rejected the request: HTTP {resp.status_code}")
                else:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        last_error = exc
                        logger.warning("Wikidata returned invalid JSON, attempt %d/%d", attempt, MAX_ATTEMPTS)
            if attempt < MAX_ATTEMPTS:
                self._sleep(BACKOFF_BASE ** attempt + random.uniform(0, 1))
        raise WikidataUnavailableError(f"Wikidata unavailable after {MAX_ATTEMPTS} attempts: {last_error}")

    def sparql(self, query: str) -> list[dict]:
        data = self._get(SPARQL_ENDPOINT, {"query": query, "format": "json"},
                         "application/sparql-results+json", method="POST")
        bindings = (data.get("results") or {}).get("bindings") if isinstance(data, dict) else None
        return bindings if isinstance(bindings, list) else []

    def search(self, term: str, limit: int = 25) -> list[str]:
        """Full-text search for companies/brands matching term; returns QIDs."""
        data = self._get(WIKIDATA_API, {
            "action": "query", "list": "search", "format": "json", "srnamespace": 0,
            "srlimit": limit, "srsearch": f"{term} {KEYWORD_P31_FILTER}",
        }, "application/json")
        hits = ((data.get("query") or {}).get("search") or []) if isinstance(data, dict) else []
        return [h["title"] for h in hits if isinstance(h, dict) and _QID.match(str(h.get("title", "")))]


def _parse_retry_after(value: str | None) -> int:
    try:
        seconds = int(float(value)) if value is not None else 30
    except ValueError:
        seconds = 30
    return max(1, min(seconds, MAX_RETRY_AFTER))


# ---------------------------------------------------------------------------
# SPARQL response helpers
# ---------------------------------------------------------------------------

def _qid(uri: str | None) -> str | None:
    """'http://www.wikidata.org/entity/Q123' -> 'Q123'; anything else -> None."""
    if not isinstance(uri, str):
        return None
    tail = uri.rsplit("/", 1)[-1]
    return tail if _QID.match(tail) else None


def _value(row: dict, key: str) -> str | None:
    cell = row.get(key) if isinstance(row, dict) else None
    val = cell.get("value") if isinstance(cell, dict) else None
    return val.strip() if isinstance(val, str) and val.strip() else None


def _split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(_SEP) if v.strip()]


def _values_block(qids) -> str:
    return " ".join(f"wd:{q}" for q in qids if _QID.match(q))


def _literal_block(labels) -> str:
    return " ".join('"' + label.replace("\\", "\\\\").replace('"', '\\"') + '"@en' for label in labels)


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    qid: str
    methods: set[str] = field(default_factory=set)
    discovery_products: set[str] = field(default_factory=set)   # product labels from strategy A
    discovery_categories: set[str] = field(default_factory=set)
    label: str | None = None
    description: str | None = None
    types: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)            # P1056
    made_products: list[str] = field(default_factory=list)       # classes of items with P176 -> this
    websites: list[str] = field(default_factory=list)
    instagram_handles: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    headquarters: list[str] = field(default_factory=list)
    operating_areas: list[str] = field(default_factory=list)
    hq_countries: list[str] = field(default_factory=list)       # countries of the P159 headquarters
    sitelinks: int = 0                                           # number of Wikipedia/Wikimedia language articles
    parents: list[str] = field(default_factory=list)
    is_business: bool = False
    dissolved: bool = False                                      # has P576 (dissolved/abolished)
    also_qids: list[str] = field(default_factory=list)


def _add(cands: dict[str, Candidate], qid: str | None, method: str,
         product: str | None = None, category: str | None = None) -> None:
    if not qid:
        return
    cand = cands.setdefault(qid, Candidate(qid=qid))
    cand.methods.add(method)
    if product:
        cand.discovery_products.add(product)
    if category:
        cand.discovery_categories.add(category)


# ---------------------------------------------------------------------------
# Strategy A / B helpers: resolve concepts
# ---------------------------------------------------------------------------

def resolve_concepts(client: WikidataClient, labels) -> dict[str, list[str]]:
    """Exact English label -> QIDs of items that are real classes (have P279)."""
    labels = list(dict.fromkeys(labels))
    rows = client.sparql(f"""
SELECT ?l ?item WHERE {{
  VALUES ?l {{ {_literal_block(labels)} }}
  ?item rdfs:label ?l .
  FILTER EXISTS {{ ?item wdt:P279 [] }}
}}""")
    out: dict[str, list[str]] = {}
    for row in rows:
        label, qid = _value(row, "l"), _qid(_value(row, "item"))
        if label and qid:
            out.setdefault(label, []).append(qid)
    return out


def expand_subclasses(client: WikidataClient, qids: list[str]) -> dict[str, tuple[str, str]]:
    """Subclasses (up to 2 hops) of the given classes -> {qid: (English label, parent concept qid)}."""
    if not qids:
        return {}
    rows = client.sparql(f"""
SELECT DISTINCT ?p ?pl ?concept WHERE {{
  VALUES ?concept {{ {_values_block(qids)} }}
  {{ ?p wdt:P279 ?concept }} UNION {{ ?p wdt:P279/wdt:P279 ?concept }}
  ?p rdfs:label ?pl . FILTER(lang(?pl) = "en")
}} LIMIT {SUBCLASS_CAP}""")
    out = {}
    for row in rows:
        qid, label, parent = _qid(_value(row, "p")), _value(row, "pl"), _qid(_value(row, "concept"))
        if qid and label and parent and qid not in out:
            out[qid] = (label, parent)
    return out


def discover_by_products(client: WikidataClient, cands: dict[str, Candidate],
                         categories: tuple[str, ...]) -> None:
    concepts = [c for c in PRODUCT_CONCEPTS if c[1] in categories]
    resolved = resolve_concepts(client, [c[0] for c in concepts])
    product_label: dict[str, str] = {}
    product_category: dict[str, str] = {}
    to_expand: list[str] = []
    for label, category, expand in concepts:
        for qid in resolved.get(label, []):
            product_label[qid], product_category[qid] = label, category
            if expand:
                to_expand.append(qid)
    for qid, (label, parent) in expand_subclasses(client, to_expand).items():
        if qid not in product_label:
            product_label[qid] = label
            product_category[qid] = product_category[parent]
    if not product_label:
        logger.warning("Strategy A: no product concepts resolved")
        return
    block = _values_block(product_label)
    logger.info("Strategy A: %d product classes", len(product_label))

    for row in client.sparql(f"""
SELECT DISTINCT ?c ?p WHERE {{ VALUES ?p {{ {block} }} ?c wdt:P1056 ?p . }} LIMIT {RESULT_CAP}"""):
        p = _qid(_value(row, "p"))
        _add(cands, _qid(_value(row, "c")), "Product relationship (company produces, P1056)",
             product_label.get(p), product_category.get(p))
    for row in client.sparql(f"""
SELECT DISTINCT ?c ?p WHERE {{ VALUES ?p {{ {block} }} ?x wdt:P31 ?p . ?x wdt:P176 ?c . }} LIMIT {RESULT_CAP}"""):
        p = _qid(_value(row, "p"))
        _add(cands, _qid(_value(row, "c")), "Product relationship (manufacturer of product, P176)",
             product_label.get(p), product_category.get(p))


def discover_by_industry(client: WikidataClient, cands: dict[str, Candidate]) -> None:
    resolved = resolve_concepts(client, INDUSTRY_CONCEPTS + COMPANY_CLASS_CONCEPTS)
    industries = [q for label in INDUSTRY_CONCEPTS for q in resolved.get(label, [])]
    classes = [q for label in COMPANY_CLASS_CONCEPTS for q in resolved.get(label, [])]
    if industries:
        for row in client.sparql(f"""
SELECT DISTINCT ?c WHERE {{ VALUES ?i {{ {_values_block(industries)} }} ?c wdt:P452 ?i . }} LIMIT {RESULT_CAP}"""):
            _add(cands, _qid(_value(row, "c")), "Industry relationship (P452)")
    if classes:
        for row in client.sparql(f"""
SELECT DISTINCT ?c WHERE {{ VALUES ?t {{ {_values_block(classes)} }} ?c wdt:P31 ?t . }} LIMIT {RESULT_CAP}"""):
            _add(cands, _qid(_value(row, "c")), "Industry relationship (instance of sporting goods manufacturer)")
    logger.info("Strategy B: %d industries, %d company classes", len(industries), len(classes))


def discover_by_keywords(client: WikidataClient, cands: dict[str, Candidate],
                         categories: tuple[str, ...]) -> None:
    for term, category in KEYWORD_TERMS:
        if category not in categories:
            continue
        for qid in client.search(term):
            _add(cands, qid, f"Keyword search ('{term}')", category=category)


# ---------------------------------------------------------------------------
# Details
# ---------------------------------------------------------------------------

def _concat_sub(block: str, var: str, pattern: str, label: bool = True) -> str:
    """OPTIONAL sub-select returning one '||'-joined string per entity (no row explosion)."""
    value = "?_lbl" if label else "STR(?_v)"
    label_clause = '?_v rdfs:label ?_lbl . FILTER(lang(?_lbl) = "en")' if label else ""
    return f"""
  OPTIONAL {{ SELECT ?e (GROUP_CONCAT(DISTINCT {value}; separator="{_SEP}") AS ?{var}) WHERE {{
      VALUES ?e {{ {block} }} {pattern} {label_clause} }} GROUP BY ?e }}"""


def fetch_details(client: WikidataClient, cands: dict[str, Candidate]) -> None:
    qids = list(cands)
    biz = _values_block(BUSINESS_TYPE_QIDS)
    for start in range(0, len(qids), DETAIL_BATCH):
        batch = qids[start:start + DETAIL_BATCH]
        block = _values_block(batch)
        query = f"""
SELECT ?e ?label ?anyLabel ?desc ?types ?inds ?prods ?made ?sites ?igs ?countries ?hqs ?hqcountries ?areas ?parents ?biz ?dissolved ?sitelinks WHERE {{
  VALUES ?e {{ {block} }}
  OPTIONAL {{ ?e rdfs:label ?label . FILTER(lang(?label) = "en") }}
  OPTIONAL {{ SELECT ?e (SAMPLE(?_al) AS ?anyLabel) WHERE {{ VALUES ?e {{ {block} }} ?e rdfs:label ?_al . }} GROUP BY ?e }}
  OPTIONAL {{ ?e schema:description ?desc . FILTER(lang(?desc) = "en") }}
  {_concat_sub(block, "types", "?e wdt:P31 ?_v .")}
  {_concat_sub(block, "inds", "?e wdt:P452 ?_v .")}
  {_concat_sub(block, "prods", "?e wdt:P1056 ?_v .")}
  {_concat_sub(block, "made", "?_m wdt:P176 ?e . ?_m wdt:P31 ?_v .")}
  {_concat_sub(block, "sites", "?e wdt:P856 ?_v .", label=False)}
  {_concat_sub(block, "igs", "?e wdt:P2003 ?_v .", label=False)}
  {_concat_sub(block, "countries", "?e wdt:P17 ?_v .")}
  {_concat_sub(block, "hqs", "?e wdt:P159 ?_v .")}
  {_concat_sub(block, "hqcountries", "?e wdt:P159 ?_h . ?_h wdt:P17 ?_v .")}
  {_concat_sub(block, "areas", "?e wdt:P2541 ?_v .")}
  {_concat_sub(block, "parents", "?e wdt:P749 ?_v .")}
  BIND(EXISTS {{ ?e wdt:P31/wdt:P279* ?_t . VALUES ?_t {{ {biz} }} }} AS ?biz)
  BIND(EXISTS {{ ?e wdt:P576 [] }} AS ?dissolved)
  OPTIONAL {{ ?e wikibase:sitelinks ?sitelinks . }}
}}"""
        for row in client.sparql(query):
            apply_detail_row(cands, row)


def apply_detail_row(cands: dict[str, Candidate], row: dict) -> None:
    """Copy one details-query row onto its Candidate (ignores malformed rows)."""
    qid = _qid(_value(row, "e"))
    cand = cands.get(qid) if qid else None
    if cand is None:
        return
    label = _value(row, "label") or _value(row, "anyLabel")
    cand.label = label if label and not _QID.match(label) else cand.label
    cand.description = _value(row, "desc") or cand.description
    cand.types = _split(_value(row, "types"))
    cand.industries = _split(_value(row, "inds"))
    cand.products = _split(_value(row, "prods"))
    cand.made_products = _split(_value(row, "made"))
    cand.websites = _split(_value(row, "sites"))
    cand.instagram_handles = _split(_value(row, "igs"))
    cand.countries = _split(_value(row, "countries"))
    cand.headquarters = _split(_value(row, "hqs"))
    cand.operating_areas = _split(_value(row, "areas"))
    cand.hq_countries = _split(_value(row, "hqcountries"))
    try:
        cand.sitelinks = int(_value(row, "sitelinks") or 0)
    except ValueError:
        cand.sitelinks = 0
    cand.parents = _split(_value(row, "parents"))
    cand.is_business = (_value(row, "biz") or "").lower() in ("true", "1")
    cand.dissolved = (_value(row, "dissolved") or "").lower() in ("true", "1")


# ---------------------------------------------------------------------------
# Extraction (kept separate so they can be improved later)
# ---------------------------------------------------------------------------

def extract_website(values: list[str]) -> str | None:
    """Best plausible http(s) official website from Wikidata P856 values, or None."""
    valid = []
    for url in values or []:
        if not isinstance(url, str):
            continue
        parsed = urlparse(url.strip())
        host = (parsed.netloc or "").lower()
        if parsed.scheme not in ("http", "https") or not host or "." not in host:
            continue
        if any(blocked in host for blocked in _BLOCKED_WEBSITE_HOSTS):
            continue
        valid.append(url.strip())
    if not valid:
        return None
    # max() keeps the first of equally ranked URLs, so Wikidata's order breaks ties.
    return max(valid, key=_website_rank)


_US_OR_ROOT_PATH = re.compile(r"^(?:|us|en-us|en_us|usa|en)$", re.I)
_COUNTRY_PATH = re.compile(r"^[a-z]{2}(?:[-_][a-z]{2})?$", re.I)


def _website_rank(url: str) -> tuple[int, int]:
    """
    Preference when Wikidata lists several official websites:
      1. .com domain   (brand.com)                         — always first
      2. other global / US / Canada domains (.org .net .io .us .ca)
      3. country-specific domains (.jp .kr .de .co.uk ...)
    Within the same domain rank, the main site or a US/English page
    (brand.com, brand.com/us, brand.com/en-us) beats another country's page
    (brand.com/jp, brand.com/kr).
    """
    domain = _extract_domain(url)
    tld = domain.rsplit(".", 1)[-1] if domain else ""
    if tld == "com":
        domain_rank = 3
    elif _website_score(url) == 2:      # .org/.net/.io/.us/.ca (shared helper)
        domain_rank = 2
    else:
        domain_rank = 1
    first_path = urlparse(url).path.strip("/").split("/")[0]
    if _US_OR_ROOT_PATH.match(first_path):
        path_rank = 2
    elif _COUNTRY_PATH.match(first_path):
        path_rank = 0
    else:
        path_rank = 1
    return domain_rank, path_rank


# Second-level labels that are part of a country suffix (brand.co.jp, brand.com.br, ...).
_COUNTRY_SUFFIX_LABELS = frozenset(["co", "com", "ne", "or", "ac", "net", "org", "gr", "go"])
COM_CHECK_TIMEOUT = 10.0


def _domain_base(domain: str) -> str | None:
    """'mikasasports.co.jp' -> 'mikasasports', 'brand.de' -> 'brand'."""
    parts = [p for p in domain.lower().split(".") if p and p != "www"]
    if len(parts) >= 3 and parts[-2] in _COUNTRY_SUFFIX_LABELS:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else None


def find_com_alternative(country_url: str, brand_name: str | None,
                         timeout: float = COM_CHECK_TIMEOUT) -> str | None:
    """
    For a brand whose only Wikidata website is a country site, try the same
    name on .com (mikasasports.co.jp -> mikasasports.com). Returns the .com
    URL only if it loads, still lands on a .com / global / US site after
    redirects, and the page mentions the brand — otherwise None. Uses the
    project's fetcher, so bot-protected sites go through Firecrawl / Jina.
    """
    base = _domain_base(_extract_domain(country_url))
    if not base:
        return None
    name_tokens = {t for t in re.findall(r"[a-z0-9]+", (brand_name or "").lower()) if len(t) >= 3}
    name_tokens |= {base}
    # Direct GET first; bot-protected sites (e.g. mikasasports.com answers 418)
    # fall back to the project's rendering fetcher (Firecrawl / Jina).
    from pipeline.helpers.http import fetch_page_with_jina_fallback

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    for candidate in (f"https://www.{base}.com/", f"https://{base}.com/"):
        page = fetch_page_with_jina_fallback(candidate, headers, timeout)
        if page is None:
            continue
        final = page.url or candidate
        if _website_rank(final)[0] < 2:          # redirected back to a country domain
            continue
        text = page.html[:300_000].lower()
        if any(token in text for token in name_tokens):
            return final
    return None


def extract_instagram(handles: list[str]) -> str | None:
    """Instagram profile URL from Wikidata P2003 usernames, or None. Never guesses."""
    for handle in handles or []:
        handle = (handle or "").strip().lstrip("@").strip("/")
        if _INSTAGRAM_HANDLE.match(handle):
            return f"https://www.instagram.com/{handle}/"
    return None


# ---------------------------------------------------------------------------
# Relevance
# ---------------------------------------------------------------------------

def categories_in(text: str | None) -> list[str]:
    """Beach categories whose vocabulary appears in text (in CATEGORIES order)."""
    if not text:
        return []
    return [cat for cat in CATEGORIES if any(p.search(text) for p in _COMPILED[cat])]


def _only_weak(text: str) -> bool:
    """True when text's beach evidence is diving/snorkel gear only."""
    strong = any(
        p.search(text) for cat in CATEGORIES for p in _COMPILED[cat]
        if p.pattern not in {w.pattern for w in _WEAK}
    )
    return not strong and any(w.search(text) for w in _WEAK)


@dataclass
class Relevance:
    level: str                  # HIGH / MEDIUM / LOW
    reason: str
    categories: list[str]


def classify_relevance(cand: Candidate) -> Relevance:
    """
    HIGH   — the description says the business itself is about surfing /
             swimming / water sports / beach sports, or at least two (and at
             least half) of its Wikidata products are beach products.
    MEDIUM — structured Wikidata evidence (products, industry, discovery
             product) shows a beach product line inside a broader business, or
             the description mentions beach products without describing the
             business; diving/snorkel-only evidence is capped here.
    LOW    — no beach evidence, a negative context (school, pool supplies,
             software, hotel, place, person, ...), dissolved/defunct (P576),
             or not a company/brand.
    The word "beach" by itself is never evidence, and the brand name is not used.
    """
    desc = cand.description or ""
    context = " ".join([desc] + cand.types)
    if not cand.is_business:
        return Relevance("LOW", "Not a company/brand on Wikidata (no business/brand/trademark type).", [])
    # P576 is often set when a corporate entity ends (e.g. acquired) while the
    # brand lives on — only treat it as defunct when Wikidata also lists no
    # website or no Instagram for it.
    active_presence = bool(extract_website(cand.websites)) and bool(extract_instagram(cand.instagram_handles))
    if DEFUNCT.search(desc) or (cand.dissolved and not active_presence):
        return Relevance("LOW", "Excluded: company is dissolved/defunct according to Wikidata.", [])
    if NEGATIVE_CONTEXT.search(context):
        match = NEGATIVE_CONTEXT.search(context).group(0)
        return Relevance("LOW", f"Excluded: description/type indicates '{match}', not a beach product brand.", [])

    dissolved_note = (" Note: Wikidata records the company as dissolved (P576), but it still lists a "
                      "website and Instagram, so the brand appears active." if cand.dissolved else "")
    rel = _classify_evidence(cand, desc)
    return Relevance(rel.level, rel.reason + dissolved_note, rel.categories) if rel.level != "LOW" else rel


def _classify_evidence(cand: Candidate, desc: str) -> Relevance:
    product_labels = cand.products + cand.made_products
    structured_text = " | ".join(product_labels + cand.industries + sorted(cand.discovery_products))
    desc_cats = categories_in(desc)
    struct_cats = categories_in(structured_text)
    beach_products = [p for p in product_labels if categories_in(p)]
    categories = list(dict.fromkeys(desc_cats + struct_cats))

    if desc_cats and BUSINESS_NOUN.search(desc):
        if _only_weak(desc) and not (struct_cats and not _only_weak(structured_text)):
            return Relevance("MEDIUM", f"Description mentions diving/snorkel gear only: \"{desc}\".", desc_cats)
        return Relevance("HIGH", f"Description identifies the business as {', '.join(CATEGORY_LABELS[c].lower() for c in desc_cats)}: \"{desc}\".", categories)
    if len(beach_products) >= 2 and len(beach_products) * 2 >= len(product_labels):
        if _only_weak(" | ".join(beach_products)):
            return Relevance("MEDIUM", f"Wikidata products are diving/snorkel gear: {', '.join(beach_products[:5])}.", struct_cats)
        return Relevance("HIGH", f"Most Wikidata products are beach/water-sports products: {', '.join(beach_products[:5])}.", categories)
    if struct_cats:
        evidence = beach_products or sorted(cand.discovery_products) or [i for i in cand.industries if categories_in(i)]
        return Relevance("MEDIUM", f"Broader business with a beach-related product line per Wikidata: {', '.join(evidence[:5])}.", categories)
    if desc_cats:
        return Relevance("MEDIUM", f"Description mentions beach products but does not describe the business clearly: \"{desc}\".", desc_cats)
    return Relevance("LOW", "No beach or water-sports evidence in Wikidata.", [])


COVERAGE_WORLDWIDE = "WORLDWIDE"
COVERAGE_US = "US_NATIONWIDE"
COVERAGE_MULTI_US = "MULTI_COUNTRY_WITH_US"
COVERAGE_UNKNOWN = "UNKNOWN"
QUALIFYING_COVERAGE = (COVERAGE_WORLDWIDE, COVERAGE_US, COVERAGE_MULTI_US)
COVERAGE_LABELS = {
    COVERAGE_WORLDWIDE: "Worldwide",
    COVERAGE_US: "United States",
    COVERAGE_MULTI_US: "Multiple countries including the US",
    COVERAGE_UNKNOWN: "Unknown",
}
_WORLD = re.compile(r"\b(?:world|worldwide|global|globally|earth|international|internationally|multinational)\b", re.I)
_US = re.compile(r"\b(?:united\s+states(?:\s+of\s+america)?|usa|u\.s\.a?\.?)\b", re.I)
# Lower-case only on purpose: "International" capitalised is usually part of a
# company name ("Tdgpeople International SDN BHD"), not a coverage statement.
_DESC_WORLD = re.compile(
    r"\b(?:multinational|global|globally|worldwide|international|internationally|"
    r"in\s+(?:over|more\s+than)\s+\d+\s+countries|sold\s+in\s+\d+\s+countries)\b")


@dataclass
class Coverage:
    level: str
    reason: str

    @property
    def text(self) -> str:
        if self.level == COVERAGE_UNKNOWN:
            return "Unknown" + (f" ({self.reason})" if self.reason else "")
        return f"{COVERAGE_LABELS[self.level]} — {self.reason} (Wikidata evidence; not independently verified)"


def _website_countries(urls: list[str]) -> set[str]:
    """Country-site markers from P856 values: ccTLD (de, jp, ...), 'us' for .us, 'global' for .com/.org/..."""
    out = set()
    for url in urls or []:
        domain = _extract_domain(url)
        if not domain:
            continue
        tld = domain.rsplit(".", 1)[-1]
        out.add("global" if tld in ("com", "org", "net", "io", "co") else tld)
        path = urlparse(url).path.strip("/").split("/")[0].lower()   # e.g. ripcurl.com/au, brand.com/en-us
        if re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", path or ""):
            out.add(path.split("-")[-1])
    return out


def classify_coverage(cand: Candidate) -> Coverage:
    """
    Market coverage from the Wikidata evidence available, strongest first:
      1. P2541 operating area — world/global -> WORLDWIDE; the US alone ->
         US_NATIONWIDE; the US plus other areas -> MULTI_COUNTRY_WITH_US.
      2. Description says multinational / global / worldwide /
         international / sold in N countries -> WORLDWIDE.
      3. Countries (P17) or headquarters countries (P159 -> P17) include the
         US and at least one other country -> MULTI_COUNTRY_WITH_US.
      4. An official US website (P856: .us domain or /us path) -> US.
      5. Country (P17) or headquarters country is the US -> US (US-based
         brand, so it operates in the US).
    Only US evidence qualifies: operating in the US, headquartered in the
    US, or an explicit worldwide / multinational statement. Wikipedia
    language count and non-US country sites are NOT evidence. Brands with
    only non-US countries on record, or no location data, are Unknown and
    filtered out by default.
    """
    areas = cand.operating_areas
    if areas:
        if any(_WORLD.search(a) for a in areas):
            return Coverage(COVERAGE_WORLDWIDE, f"operating area: {', '.join(areas)}")
        us_areas = [a for a in areas if _US.search(a)]
        if us_areas and len(areas) == len(us_areas):
            return Coverage(COVERAGE_US, f"operating area: {', '.join(areas)}")
        if us_areas:
            return Coverage(COVERAGE_MULTI_US, f"operating area: {', '.join(areas)}")

    match = _DESC_WORLD.search(cand.description or "")
    if match:
        return Coverage(COVERAGE_WORLDWIDE, f"description says '{match.group(0)}'")

    countries = list(dict.fromkeys(cand.countries + cand.hq_countries))
    if len(countries) >= 2 and any(_US.search(c) for c in countries):
        return Coverage(COVERAGE_MULTI_US, f"countries / headquarters in: {', '.join(countries)}")

    # An official US website (a .us domain or a /us path) is direct evidence
    # of US operation. Country sites for OTHER countries are not.
    site_countries = _website_countries(cand.websites)
    if "us" in site_countries:
        return Coverage(COVERAGE_US, f"official US website ({', '.join(sorted(site_countries))})")

    # Deliberately NO "Wikipedia articles in N languages" or "websites for
    # several countries" fallback: being well known abroad (Arena, Rip Curl,
    # Ray-Ban...) says nothing about operating in or being headquartered in
    # the US, so such brands are Unknown and filtered out.

    if countries and not any(_US.search(c) for c in countries):
        return Coverage(COVERAGE_UNKNOWN, f"only non-US country on record: {', '.join(countries)}")
    if countries:
        # A US-based brand operates in the US — that is all this discovery
        # needs (beach-related brands in any niche that operate in the US).
        return Coverage(COVERAGE_US, f"US-based — country / headquarters: {', '.join(countries)}")
    return Coverage(COVERAGE_UNKNOWN, "no location data on Wikidata")


def market_coverage(cand: Candidate) -> str:
    return classify_coverage(cand).text


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

@dataclass
class BrandResult:
    brand: str
    wikidata_id: str
    description: str | None
    website: str | None
    website_source: str | None
    instagram: str | None
    instagram_source: str | None
    beach_categories: list[str]
    beach_relevance: str
    relevance_reason: str
    country: list[str]
    headquarters: list[str]
    parent_organization: list[str]
    market_coverage: str
    coverage_level: str
    discovery_methods: list[str]
    also_wikidata_ids: list[str]
    entity_type: str | None = None        # label of the first P31 (instance-of) value
    operating_area: list[str] = field(default_factory=list)   # P2541
    _evidence: int = 0

    def to_json(self) -> dict:
        data = asdict(self)
        data.pop("_evidence", None)
        data["wikidata_url"] = f"https://www.wikidata.org/wiki/{self.wikidata_id}"
        return data


def build_results(cands: dict[str, Candidate], category: str = "all",
                  min_relevance: str = "MEDIUM", all_coverage: bool = False) -> list[BrandResult]:
    allowed = {"HIGH"} if min_relevance == "HIGH" else {"HIGH", "MEDIUM"}
    results: list[BrandResult] = []
    for cand in cands.values():
        rel = classify_relevance(cand)
        if rel.level not in allowed:
            continue
        if category != "all" and category not in rel.categories:
            continue
        coverage = classify_coverage(cand)
        if not all_coverage and coverage.level not in QUALIFYING_COVERAGE:
            continue
        website = extract_website(cand.websites)
        website_source = "Wikidata P856 (not independently verified)" if website else None
        if website and _website_rank(website)[0] == 1:          # only a country site on Wikidata
            com = find_com_alternative(website, cand.label)
            if com:
                logger.info("%s: Wikidata only lists %s — using %s (loads and mentions the brand)",
                            cand.label or cand.qid, website, com)
                website_source = (f"derived .com of the Wikidata website {website} — checked that it loads "
                                  f"and mentions the brand (not independently verified)")
                website = com
        instagram = extract_instagram(cand.instagram_handles)
        results.append(BrandResult(
            brand=cand.label or cand.qid,
            wikidata_id=cand.qid,
            description=cand.description,
            website=website,
            website_source=website_source,
            instagram=instagram,
            instagram_source="Wikidata P2003 (not independently verified)" if instagram else None,
            beach_categories=rel.categories,
            beach_relevance=rel.level,
            relevance_reason=rel.reason,
            country=cand.countries,
            headquarters=cand.headquarters,
            parent_organization=cand.parents,
            market_coverage=coverage.text,
            coverage_level=coverage.level,
            discovery_methods=sorted(cand.methods),
            also_wikidata_ids=list(cand.also_qids),
            entity_type=(cand.types[0] if cand.types else None),
            operating_area=list(cand.operating_areas),
            _evidence=len(cand.methods) + len(cand.discovery_products) + bool(website) + bool(instagram),
        ))
    results = dedupe_by_domain(results)
    results.sort(key=lambda r: (r.beach_relevance != "HIGH", -r._evidence, r.brand.lower()))
    return results


def dedupe_by_domain(results: list[BrandResult]) -> list[BrandResult]:
    """Keep one result per official-website domain (the stronger one); others become aliases."""
    by_domain: dict[str, BrandResult] = {}
    out: list[BrandResult] = []
    for res in sorted(results, key=lambda r: (r.beach_relevance != "HIGH", -r._evidence)):
        domain = _extract_domain(res.website) if res.website else ""
        if domain and domain in by_domain:
            keep = by_domain[domain]
            keep.also_wikidata_ids.append(res.wikidata_id)
            keep.discovery_methods = sorted(set(keep.discovery_methods) | set(res.discovery_methods))
            continue
        if domain:
            by_domain[domain] = res
        out.append(res)
    return out


def discover(client: WikidataClient, category: str = "all", min_relevance: str = "MEDIUM",
             all_coverage: bool = False) -> list[BrandResult]:
    """Run all strategies, fetch details, classify and dedupe. QID dedupe is built into _add()."""
    categories = CATEGORIES if category == "all" else (category,)
    cands: dict[str, Candidate] = {}
    discover_by_products(client, cands, categories)
    discover_by_industry(client, cands)
    discover_by_keywords(client, cands, categories)
    logger.info("Merged %d unique candidate QIDs — fetching details", len(cands))
    fetch_details(client, cands)
    return build_results(cands, category=category, min_relevance=min_relevance, all_coverage=all_coverage)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _or_unavailable(values, what: str = "Not available") -> str:
    if isinstance(values, list):
        return ", ".join(values) if values else what
    return values or what


def render(results: list[BrandResult], limit: int | None, category: str, min_relevance: str,
           all_coverage: bool = False) -> str:
    line, thin = "=" * 60, "-" * 60
    out = [line, "WIKIDATA BEACH-RELATED FITNESS BRAND DISCOVERY", line, "",
           f"Requested limit: {limit if limit is not None else 'none (all brands)'}", "Discovery source: Wikidata", "Niche: Fitness",
           "Focus: Beach and water-sports-related brands",
           f"Category filter: {category}", f"Minimum relevance: {min_relevance}",
           "Market coverage filter: " + ("none (--all-coverage)" if all_coverage else
                                         "worldwide, US nationwide, or multiple countries including the US"),
           ""]
    for i, r in enumerate(results, 1):
        geo = []
        if r.country:
            geo.append(f"country {', '.join(r.country)}")
        if r.headquarters:
            geo.append(f"headquarters {', '.join(r.headquarters)}")
        out += [thin, f"BRAND {i}", thin,
                f"Brand: {r.brand}",
                f"Wikidata ID: {r.wikidata_id}",
                f"Description: {_or_unavailable(r.description)}",
                f"Website: {r.website or 'Not found'}" + (f"  [{r.website_source}]" if r.website else ""),
                f"Instagram: {r.instagram or 'Not found'}" + (f"  [{r.instagram_source}]" if r.instagram else ""),
                f"Beach category: {', '.join(CATEGORY_LABELS[c] for c in r.beach_categories) or 'Not determined'}",
                f"Beach relevance: {r.beach_relevance}",
                f"Relevance reason: {r.relevance_reason}",
                f"Geography: {'; '.join(geo) if geo else 'Not available'}",
                f"Parent organization: {_or_unavailable(r.parent_organization)}",
                f"Market coverage: {r.market_coverage}",
                f"Discovery method: {'; '.join(r.discovery_methods)}"]
        if r.also_wikidata_ids:
            out.append(f"Also on Wikidata as: {', '.join(r.also_wikidata_ids)} (same website)")
        out.append("")
    out += [line, "SUMMARY", line, ""]
    if limit is not None and len(results) < limit:
        out.append(f"Only {len(results)} brand(s) met the criteria (requested {limit}).")
    out += [f"Brands discovered: {len(results)}",
            f"Brands with websites: {sum(1 for r in results if r.website)}",
            f"Brands with Instagram accounts: {sum(1 for r in results if r.instagram)}",
            f"High relevance: {sum(1 for r in results if r.beach_relevance == 'HIGH')}",
            f"Medium relevance: {sum(1 for r in results if r.beach_relevance == 'MEDIUM')}",
            f"Worldwide: {sum(1 for r in results if r.coverage_level == COVERAGE_WORLDWIDE)}",
            f"US nationwide: {sum(1 for r in results if r.coverage_level == COVERAGE_US)}",
            f"Multiple countries incl. US: {sum(1 for r in results if r.coverage_level == COVERAGE_MULTI_US)}"]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _limit_arg(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--limit must be a whole number, got {value!r}")
    if n < 1:
        raise argparse.ArgumentTypeError("--limit must be 1 or more")
    return n


def _timeout_arg(value: str) -> float:
    try:
        t = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--timeout must be a number of seconds, got {value!r}")
    if not 1 <= t <= 300:
        raise argparse.ArgumentTypeError("--timeout must be between 1 and 300 seconds")
    return t


def _relevance_arg(value: str) -> str:
    v = value.strip().upper()
    if v not in ("HIGH", "MEDIUM"):
        raise argparse.ArgumentTypeError("--min-relevance must be HIGH or MEDIUM")
    return v


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover beach / water-sports fitness brands from Wikidata (prints to terminal; no database writes).")
    parser.add_argument("--limit", type=_limit_arg, default=None,
                        help="Max number of unique brands to display (default: all of them).")
    parser.add_argument("--category", default="all", choices=("all",) + CATEGORIES,
                        help="Restrict to one beach category (default all).")
    parser.add_argument("--min-relevance", type=_relevance_arg, default="MEDIUM",
                        help="Minimum relevance: HIGH or MEDIUM (default MEDIUM).")
    parser.add_argument("--timeout", type=_timeout_arg, default=DEFAULT_TIMEOUT,
                        help=f"HTTP timeout per request in seconds (default {DEFAULT_TIMEOUT:g}).")
    parser.add_argument("--output", type=Path, default=None, help="Optional JSON output file path.")
    parser.add_argument("--all-coverage", action="store_true",
                        help="Also show brands whose market coverage is unknown or non-US "
                             "(default: only worldwide / US nationwide / multi-country incl. US).")
    return parser


def main(argv: list[str] | None = None, client: WikidataClient | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.output is not None:
        if args.output.suffix.lower() != ".json":
            parser.error("--output must be a .json file path")
        if not args.output.resolve().parent.is_dir():
            parser.error(f"--output directory does not exist: {args.output.resolve().parent}")

    own_client = client is None
    client = client or WikidataClient(timeout=args.timeout)
    try:
        results = discover(client, category=args.category, min_relevance=args.min_relevance,
                           all_coverage=args.all_coverage)
    except WikidataUnavailableError as exc:
        print(f"ERROR: Wikidata is unavailable — {exc}", file=sys.stderr)
        return EXIT_WIKIDATA_UNAVAILABLE
    finally:
        if own_client:
            client.close()

    shown = results if args.limit is None else results[:args.limit]
    print(render(shown, args.limit, args.category, args.min_relevance, args.all_coverage))
    logger.info("%d qualifying brand(s) in total, %d shown; %d Wikidata request(s)",
                len(results), len(shown), client.requests_made)
    if args.output is not None:
        payload = {
            "query": {"limit": args.limit, "category": args.category, "min_relevance": args.min_relevance,
                      "all_coverage": args.all_coverage, "source": "Wikidata"},
            "brands": [r.to_json() for r in shown],
            "summary": {
                "brands_discovered": len(shown),
                "brands_with_websites": sum(1 for r in shown if r.website),
                "brands_with_instagram": sum(1 for r in shown if r.instagram),
                "high_relevance": sum(1 for r in shown if r.beach_relevance == "HIGH"),
                "medium_relevance": sum(1 for r in shown if r.beach_relevance == "MEDIUM"),
            },
        }
        args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nJSON written to {args.output}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
