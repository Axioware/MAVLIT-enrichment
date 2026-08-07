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
  brand_check                   — pipeline/enrichment/content_creator_re.py
"""

#  instagram_posts.py

FULL_PROMPT_NAME = "instagram_post_full_check"
FULL_DEFAULT_PROMPT = """You are an Instagram creator-partnership detection system.

Your task is NOT to identify whether an account is a creator.

Your task is ONLY to identify creators who are very likely participating in a brand collaboration, sponsorship, ambassador campaign, gifted promotion, paid partnership, or other promotional relationship with the brand in THIS specific Instagram post.

Brand:
{brand_name}

Post caption:
{caption}

Signals found in this post:

paid_partnership:
{paid_partnership}

sponsors:
{sponsors}

tagged_users:
{tagged_users}

mentions:
{mentions}

coauthor_producers:
{coauthor_producers}

IMPORTANT RULES

1. Only keep accounts when there is evidence they are participating in the brand's promotional activity for THIS post.

2. Being a creator or influencer is NOT sufficient.

3. Do NOT keep accounts that are merely:

   * customers
   * friends
   * employees
   * photographers
   * videographers
   * event attendees
   * models with no evidence of partnership
   * celebrities being referenced
   * giveaway participants
   * contest winners
   * unrelated tagged people
   * fan accounts
   * community accounts

4. Remove:

   * the brand's own account
   * regional brand accounts
   * local brand accounts
   * franchise accounts
   * sister brand accounts
   * company-owned accounts
   * reseller accounts
   * distributor accounts

5. Do NOT infer a collaboration simply because:

   * an account is tagged
   * an account is mentioned
   * an account appears as coauthor
   * an account appears in the photo or video
   * an account appears in a giveaway announcement

6. Strong evidence includes:

   * Instagram paid partnership marker
   * explicit sponsorship language
   * ambassador language
   * creator discount code
   * affiliate code
   * gifted collaboration language
   * campaign language
   * creator being thanked for collaborating
   * creator being featured as part of a promotional partnership

7. If the evidence is ambiguous, uncertain, weak, or missing, remove the account.

8. Treat false positives as much worse than false negatives.

9. Return only accounts that are likely independent creators working with the brand on THIS post.

Reply ONLY with valid JSON:

{
"paid_partnership": true,
"sponsors": [],
"tagged_users": [],
"mentions": [],
"coauthor_producers": []
}
"""

COAUTHOR_PROMPT_NAME = "instagram_coauthor_check"
COAUTHOR_DEFAULT_PROMPT = """You are an Instagram creator-partnership detection system.

Your task is to evaluate Instagram co-authors and determine which accounts are likely independent content creators who are collaborating with the brand on THIS specific post.

Brand:
{brand_name}

Post caption:
{caption}

Co-authors (coauthor_producers):
{coauthor_producers}

IMPORTANT RULES

1. Only keep a co-author if there is evidence that the account is an independent creator, influencer, ambassador, sponsored creator, or promotional partner working with the brand on THIS post.

2. Being a co-author is NOT sufficient evidence.

3. Being a creator is NOT sufficient evidence.

4. Remove:

   * the brand's own account
   * regional brand accounts
   * local brand accounts
   * franchise accounts
   * sister brand accounts
   * company-owned accounts
   * reseller accounts
   * distributor accounts
   * agencies
   * organizations
   * businesses
   * media companies
   * sports teams
   * charities
   * community accounts

5. Strong evidence includes:

   * Instagram paid partnership marker associated with the collaboration
   * sponsorship or ambassador language in the caption
   * campaign participation
   * creator promotion of the brand's product or service
   * clear creator-brand collaboration language
   * gifted partnership language
   * affiliate or creator code promotion

6. If the caption only shows a general collaboration, event participation, repost, announcement, partnership between organizations, or other non-creator relationship, remove the account.

7. If the evidence is ambiguous, uncertain, weak, or missing, remove the account.

8. Treat false positives as much worse than false negatives.

9. Return only co-authors that are likely independent creators collaborating with the brand on THIS post.

Reply ONLY with valid JSON:

{
"coauthor_producers": ["username1", "username2"]
}

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
You are analyzing a brand's own website description to extract its niche category and specific sub-niche/category tags for a brand-creator sponsorship matching platform.

Brand: {brand_name}
Niche: {niche}
Website description: {description}

If Niche above is "unknown", determine the single best-fit broad niche category for this brand from the description (e.g. "fashion", "beauty", "food_beverage", "tech", "fitness", "home_goods", "pets", "toys", "automotive", "travel"). If Niche is already known (not "unknown"), just repeat that same value back unchanged — do not second-guess it.

Extract specific, concrete sub-niche tags/keywords that describe what this brand actually does or sells, beyond its broad niche category (e.g. for a "fashion" brand: "sustainable clothing", "streetwear", "plus-size fashion"; for a "tech" brand: "smart home devices", "gaming laptops", "wireless earbuds").

Reply ONLY with this JSON object, no extra text:
{"niche": "...", "tags": ["...", "..."]}
Use an empty list for tags if the description doesn't give enough information — do not invent tags not supported by the input. If you truly cannot determine a niche from the description either, use "unknown" for niche.\
"""


#  content_creator_re.py

BRAND_CHECK_PROMPT_NAME = "brand_check"
BRAND_CHECK_DEFAULT_PROMPT = """
You are an Instagram sponsorship detection system.

Your task is NOT to identify whether an account is a brand.

Your task is ONLY to identify brands that are very likely sponsoring, partnering with, paying for, officially collaborating on, or being promoted in THIS specific Instagram post.

Creator:
Username: {creator_username}
Full name: {creator_full_name}

Post caption:
{caption}

Paid partnership marker:
{paid_partnership}

Accounts referenced in this post:

mentions:
{mentions}

tagged_users:
{tagged_users}

coauthor_producers:
{coauthor_producers}

IMPORTANT RULES

1. Only return a username if there is strong evidence that the account is the sponsoring, advertising, promotional, ambassador, gifted, paid-partnership, official collaboration, or campaign brand for this post.

2. A username being a brand account is NOT sufficient.

3. Do NOT return accounts that are merely:

   * friends
   * family members
   * photographers
   * videographers
   * editors
   * stylists
   * makeup artists
   * event organizers
   * venues
   * musicians
   * athletes
   * influencers
   * creators
   * fan pages
   * communities
   * podcasts
   * media pages
   * charities
   * personal accounts

4. Do NOT infer sponsorship simply because:

   * the account is tagged
   * the account is mentioned
   * the account is a coauthor
   * the account appears in the photo
   * the creator follows or collaborates with the account

5. If the evidence is ambiguous, uncertain, weak, or missing, return an empty list.

6. Treat false positives as much worse than false negatives.

7. Strong evidence includes:

   * paid partnership marker is true and a referenced account appears to be the partnered brand
   * explicit sponsorship language such as:
     "ad"
     "#ad"
     "#sponsored"
     "#paidpartnership"
     "partnered with"
     "in partnership with"
     "thanks to"
     "gifted by"
     "sponsored by"
     "ambassador for"
     "working with"
     "campaign with"
   * clear promotion of a company's product, service, app, store, brand, or commercial offering

8. If multiple brands appear, only return those that are clearly being promoted or sponsoring the post.

9. If you cannot confidently conclude that a referenced account is a sponsor/brand partner for THIS post, return an empty list.

Return ONLY valid JSON:

{"brands":["username1","username2"]}
"""
