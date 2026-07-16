"""
pipeline/helpers/prompts.py

Default text for every LLM prompt in the pipeline that's backed by the
`prompts` database table (Prompt model). These are only ever used as a
fallback — each enrichment module's _get_..._prompt(db) helper looks up
the live row by name first and only falls back to the constant here if no
row exists yet (e.g. a brand-new database before the first migration seeds
it, or a Prompt row that got deleted). Editing these constants does NOT
change any prompt already saved in the database — that's the whole point
of storing them there instead of hardcoding: they're meant to be
customized live via the admin view (/admin/prompt) without needing a code
change or deploy.

Prompt name -> which enrichment module actually calls it:
  instagram_post_full_check     — pipeline/enrichment/instagram_posts.py (full LLM mode)
  instagram_coauthor_check      — pipeline/enrichment/instagram_posts.py (always active)
  instagram_user_demographics   — pipeline/enrichment/instagram_users.py
  instagram_creator_niche       — pipeline/enrichment/instagram_users.py (creators only, not commenters)
  youtube_commenter_gender      — pipeline/enrichment/youtube_sponsorship.py
  youtube_sponsor_check         — pipeline/enrichment/youtube_sponsorship.py
  apollo_contact_check          — pipeline/enrichment/apollo_contacts.py
  creator_content_tags          — pipeline/enrichment/creator_signals.py
  brand_niche_tags              — pipeline/enrichment/shopify_detect.py
"""

#  instagram_posts.py

FULL_PROMPT_NAME = "instagram_post_full_check"
FULL_DEFAULT_PROMPT = """\
You are filtering an Instagram post's collaboration signals to remove false positives.
Only keep users/accounts that are REAL paid or gifted content creators working with the brand.

Brand: {brand_name}
Post caption: {caption}

Signals found in this post:
paid_partnership: {paid_partnership}
sponsors: {sponsors}
tagged_users: {tagged_users}
mentions: {mentions}
coauthor_producers: {coauthor_producers}

REMOVE from each list:
- Regional or sister accounts of "{brand_name}" (brand_uk, brand_us, brand_official, etc.)
- The brand's own accounts in any form
- Automated, bot, or spam accounts
- People tagged who are clearly not independent content creators or influencers

KEEP:
- Real influencers, bloggers, or content creators with their own audience
- Brand ambassadors who were paid or gifted
- Genuine paid partnership or sponsorship markers

Reply ONLY with this JSON object (use empty list or false for fields with nothing left):
{"paid_partnership": true or false, "sponsors": [], "tagged_users": [], "mentions": [], "coauthor_producers": []}\
"""

COAUTHOR_PROMPT_NAME = "instagram_coauthor_check"
COAUTHOR_DEFAULT_PROMPT = """\
You are evaluating Instagram co-authors of a post to identify real paid content creators.

Brand: {brand_name}
Post caption: {caption}
Co-authors (coauthorProducers): {coauthor_producers}

Keep ONLY the co-authors who are real independent content creators or influencers
who were paid or gifted by "{brand_name}".

REMOVE:
- Regional or sister accounts of "{brand_name}" (brand_uk, brand_us, brand_official, etc.)
- The brand's own accounts in any form
- Any account that is clearly not an independent creator

Reply ONLY with this JSON object (empty list if none confirmed):
{"coauthor_producers": [...]}\
"""


#  instagram_users.py

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
5. age_group    — one of "12_16", "17_22", "23_28", "29_35", "36_45", "46_60", "60_plus", or "unknown"

Reply ONLY with a JSON object, no extra text:
{"gender": "...", "country": "...", "language": "...", "location": "...", "age_group": "..."}\
"""

CREATOR_NICHE_PROMPT_NAME = "instagram_creator_niche"
CREATOR_NICHE_DEFAULT_PROMPT = """\
You are classifying the content niche of an Instagram creator based on their bio and recent posts. Creators only — never used for commenters.

Bio: {bio}
Recent post captions: {captions}
Recent post hashtags: {hashtags}

Based on the bio, captions, and hashtags, identify the single most likely content niche/category this creator posts about (e.g. "fashion", "gaming", "fitness", "food_cooking", "beauty", "travel", "tech", "music", "parenting", "finance"). If there isn't enough information to tell, answer "unknown".

Reply ONLY with this JSON object, no extra text:
{"niche": "..."}\
"""


#  youtube_sponsorship.py

GENDER_PROMPT_NAME = "youtube_commenter_gender"
GENDER_DEFAULT_PROMPT = """\
You are classifying the likely gender of YouTube commenters based on their display names.

Names (JSON array, in order):
{names}

For each name, classify as "male", "female", or "unknown". Many will be usernames/handles with no clear gender signal (e.g. "xXGamerXx123", "TechReviews99", a channel name) — use "unknown" for those rather than guessing.

Reply ONLY with this JSON object, no extra text:
{"genders": ["male", "unknown", "female", ...]}
The genders array must have exactly as many entries as the input names, in the same order.\
"""

SPONSOR_CHECK_PROMPT_NAME = "youtube_sponsor_check"
SPONSOR_CHECK_DEFAULT_PROMPT = """\
You are verifying whether a YouTube video is a genuine brand sponsorship or a false positive.

Brand: {brand_name}
Detected sponsorship type: {detected_type}
Video title: {title}

Video description (first 1500 chars):
{description}

---

Is this video genuinely sponsored by or affiliated with "{brand_name}"?

Answer with ONLY this format:
RESULT: YES or NO
REASON: one short sentence explaining why\
"""


#  apollo_contacts.py

APOLLO_RANK_PROMPT_NAME = "apollo_contact_check"
APOLLO_RANK_DEFAULT_PROMPT = """\
You are a sponsorship-outreach research assistant. {intro}

You will receive a JSON list of employees (id, name, job title, and whether Apollo has an email/phone on file). Rank ALL of them from most to least likely to personally own or influence this decision — do not omit anyone, even weak fits; just rank those lower.
{title_hint}
All else equal, prefer candidates with has_email=true.

Candidates:
{candidates}

Reply ONLY with this JSON object, ranking EVERY candidate above, best first, with a short one-line reason each (a few words is fine for lower-ranked ones):
{"picks": [{"id": "...", "reason": "short one-line reason"}, ...]}
Every id from the candidate list above must appear exactly once in "picks".\
"""


#  creator_signals.py

TAGS_PROMPT_NAME = "creator_content_tags"
TAGS_DEFAULT_PROMPT = """\
You are analyzing a content creator's profile to extract matching signal for a brand-sponsorship platform.

Niche: {niche}
Sub-niches: {sub_niches}
Creator's own description: {content_description}

Extract two things from this:
1. content_tags — specific, concrete topics/themes this creator's content actually covers (e.g. "meal prep", "budget travel", "indie game reviews"). Avoid vague tags like "lifestyle" or "content creator" unless nothing more specific applies.
2. audience_value_keywords — words/phrases describing what this creator's audience cares about or values (e.g. "sustainability", "affordability", "authenticity", "family-friendly").

Reply ONLY with this JSON object, no extra text:
{"content_tags": ["...", "..."], "audience_value_keywords": ["...", "..."]}
Use empty lists if there isn't enough information to extract either one — do not invent tags not supported by the input.\
"""


#  shopify_detect.py

BRAND_NICHE_TAGS_PROMPT_NAME = "brand_niche_tags"
BRAND_NICHE_TAGS_DEFAULT_PROMPT = """\
You are analyzing a brand's own website description to extract specific sub-niche/category tags for a brand-creator sponsorship matching platform.

Brand: {brand_name}
Niche: {niche}
Website description: {description}

Extract specific, concrete sub-niche tags/keywords that describe what this brand actually does or sells, beyond its broad niche category (e.g. for a "fashion" brand: "sustainable clothing", "streetwear", "plus-size fashion"; for a "tech" brand: "smart home devices", "gaming laptops", "wireless earbuds").

Reply ONLY with this JSON object, no extra text:
{"tags": ["...", "..."]}
Use an empty list if the description doesn't give enough information — do not invent tags not supported by the input.\
"""
