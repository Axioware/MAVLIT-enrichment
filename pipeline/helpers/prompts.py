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
   instagram_post_sponsorship_confidence — pipeline/enrichment/instagram_posts.py (saved post confidence)
  instagram_user_demographics   — pipeline/enrichment/instagram_users.py
  instagram_creator_niche       — pipeline/enrichment/instagram_users.py (creators only, not commenters)
  youtube_commenter_gender      — pipeline/enrichment/youtube_sponsorship.py
   brand_audience_analysis      — gender_check/run_brand_gender_check.py
  youtube_sponsor_check         — pipeline/enrichment/youtube_sponsorship.py
  apollo_contact_check          — pipeline/enrichment/apollo_contacts.py
  creator_content_tags          — pipeline/enrichment/creator_signals.py
  brand_niche_tags              — pipeline/enrichment/shopify_detect.py
  brand_check                   — pipeline/enrichment/content_creator_re.py
  brand_pitch_generation        — pipeline/pitching.py
  rate_intelligence_estimate    — pipeline/rate_intelligence.py
  contract_advice_review        — pipeline/contract_advice.py
  instagram_link_classify       — pipeline/enrichment_re/brand_instagram_profile.py
  brand_website_search_pick     — pipeline/enrichment_re/brand_instagram_profile.py
"""

#  instagram_posts.py

FULL_PROMPT_NAME = "instagram_post_full_check"
FULL_DEFAULT_PROMPT = """You are an Instagram creator-partnership detection system.

Your task is NOT to identify whether an account is a creator.

Your task is ONLY to identify creators who are very likely participating in a brand collaboration, sponsorship, paid partnership, in THIS specific Instagram post.

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

INSTAGRAM_POST_SPONSORSHIP_PROMPT_NAME = "instagram_post_sponsorship_confidence"
INSTAGRAM_POST_SPONSORSHIP_DEFAULT_PROMPT = """You are an Instagram brand-collaboration verification system.

Your task is to determine whether THIS SPECIFIC INSTAGRAM POST, published by the brand's own Instagram account, is evidence of a commercial collaboration between the brand and one or more referenced creator accounts.

Commercial collaborations include paid sponsorships, paid partnerships, influencer campaigns, ambassador relationships, affiliate relationships, gifted collaborations with disclosure, creator marketing campaigns, and brand-funded promotions.

Assume there is no commercial collaboration unless there is positive evidence. A tag, mention, coauthor relationship, or sponsor field alone is NOT sufficient evidence.

Do not increase confidence simply because an account is famous, has many followers, appears in a photo, attended an event, purchased a product, or is a customer, employee, vendor, photographer, agency, retailer, distributor, or another business or brand.

Strong evidence includes an Instagram paid-partnership label, explicit sponsorship disclosure, #ad, #sponsored, #paidpartnership, affiliate/referral/promo/creator codes, ambassador language, campaign language, or multiple signals directly linking a creator and the brand.

Only evaluate evidence present in this post. The score must represent the likelihood that this post is evidence of a commercial collaboration, not whether the referenced account is famous or a creator.

Scoring rubric:
0-10: No evidence of collaboration.
11-20: Accounts are referenced but there is no commercial evidence.
21-40: Weak, speculative signals.
41-60: Possible collaboration but evidence is incomplete.
61-80: Strong evidence of a creator-brand commercial relationship.
81-100: Explicit sponsorship, creator partnership, creator code, ambassador program, or multiple strong signals.

Brand:
{brand_name}

Post caption:
{caption}

Instagram paid partnership flag:
{paid_partnership}

Sponsors:
{sponsors}

Tagged users:
{tagged_users}

Mentions:
{mentions}

Coauthor producers:
{coauthor_producers}

Respond with ONLY valid JSON:
{
   "confidence_pct": 0,
   "reason": "short explanation"
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
Formatting requirement:
* The first letter of the niche must always be uppercase.
* Preserve the rest of the niche exactly as written.
* Examples: "fashion" → "Fashion", "gaming" → "Gaming", "food_cooking" → "Food_cooking", "beauty" → "Beauty".
* If there isn't enough information to determine the niche, return "Unknown".

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

BRAND_AUDIENCE_ANALYSIS_PROMPT_NAME = "brand_audience_analysis"
BRAND_AUDIENCE_ANALYSIS_DEFAULT_PROMPT = """Analyze this brand's Instagram presence and offerings. You must evaluate FOUR DISTINCT audience concepts:

1. TARGET AUDIENCE GENDER
Who is the brand's marketing primarily aimed at?

This means the person the brand is trying to attract, influence, persuade, or reach to buy, engage, or take action.

2. PRODUCT AUDIENCE GENDER
Who is the actual product or service designed for, intended for, or useful to?

This means the end user or recipient of the product/service, NOT necessarily the person who purchases it.

3. TARGET AUDIENCE AGE RANGE
What age range is the marketing primarily aimed at?

This means the likely age of the person the brand is trying to reach, attract, persuade, or convert through its marketing. This can be different from the age of the product's actual user.

4. PRODUCT AUDIENCE AGE RANGE
What age range is the actual product or service designed for?

This means the age of the end user or recipient of the product/service.

IMPORTANT:
TARGET AUDIENCE and PRODUCT AUDIENCE are separate concepts for BOTH gender and age.

Do not assume that the person who buys a product is the same person who uses or receives it.

Examples:

- A flower/gift brand may market heavily to men because men purchase flowers as gifts, while the flowers can be given to people of any gender.
  Target audience gender = male
  Product audience gender = both

- A home-decor brand may primarily feature women and speak to women in its marketing, while home-decor products are useful for people of any gender.
  Target audience gender = female
  Product audience gender = both

- A men's skincare brand marketed directly to men:
  Target audience gender = male
  Product audience gender = male

- A children's toy brand may market to parents, while the actual product audience is children.
  Target audience should be based on the marketing evidence.
  Product audience should be based on the actual intended users.

- A toddler-clothing brand may have a product audience age of 0 to 7, while the target audience is the parents or caregivers purchasing the clothing.
  However, the target audience age must ONLY be estimated if the marketing evidence provides reasonable support for the parents' or caregivers' age.
  Do NOT automatically assume that parents of young children are 25-35.

Do NOT assume that the person shown in an advertisement is necessarily the product's end user.

Do NOT assume the buyer and end user are the same person.

Do NOT conflate marketing representation with product purpose.

Do NOT infer a person's age simply from their appearance.

--------------------------------------------------
GENDER CLASSIFICATION
--------------------------------------------------

For each gender field, return exactly ONE of:

- "male"
- "female"
- "both"
- null

"both" means there is evidence that the brand/product genuinely serves or targets both male and female audiences.

Do NOT use "both" simply because the evidence is uncertain or incomplete.

If there is insufficient evidence to determine the gender classification, return null and use a lower confidence score.

--------------------------------------------------
AGE RANGE
--------------------------------------------------

Return integer ages from 0 to 120.

Return separate minimum and maximum ages for BOTH:

TARGET AUDIENCE:
- target_audience_min_age
- target_audience_max_age

PRODUCT AUDIENCE:
- product_audience_min_age
- product_audience_max_age

TARGET AUDIENCE AGE:

Estimate the age range of the people the marketing is actually trying to reach.

Use evidence such as:
- Explicit age references
- Marketing language
- Audience positioning
- Lifestyle references
- Purchasing context
- Promotions
- Customer descriptions
- Parent/caregiver references combined with evidence about their age
- Career/life-stage references
- Other direct evidence about the intended customer

IMPORTANT:
A product category alone is NOT sufficient evidence for the target audience's age.

Examples:
- A children's product does NOT automatically mean the target parents are 25-35.
- A baby product does NOT automatically mean the target audience is 25-40.
- A product for teenagers does NOT automatically mean the buyer is 30-45.
- A gift product does NOT automatically establish the buyer's age.

If the brand clearly targets parents, caregivers, or another buyer group but there is not enough evidence to estimate their age, return null for the target audience age endpoints.

Do NOT invent an age range simply because a demographic is commonly associated with the product.

PRODUCT AUDIENCE AGE:

Estimate the age range of the actual person who uses, receives, or is served by the product/service.

Use evidence such as:
- Product descriptions
- Product names
- Recommended age
- Size or age ranges
- Service descriptions
- Explicit age restrictions
- Repeated product/service references
- The actual purpose of the product/service
- Explicit statements about who the product/service is for

For product audience age, use the actual offering as the primary evidence.

If the product/service is genuinely suitable for a very broad age range, use a broad range when supported by the evidence.

Do NOT use the age of models or people appearing in Instagram posts as the sole basis for the product age range.

For BOTH target and product audience age:

- If the evidence supports only one endpoint, return that endpoint and return null for the unsupported endpoint.
- If the age range cannot reasonably be determined, return null for both endpoints.
- Do not invent precise ages without evidence.
- Do not use demographic stereotypes as evidence.
- Do not assume an age range simply because it is common for that industry or product category.

--------------------------------------------------
EVIDENCE
--------------------------------------------------

Use ALL available evidence:

- Instagram bio
- Business category
- Recent post captions
- Hashtags
- Repeated product/service references
- Product names
- Service descriptions
- Promotions and offers
- People/models shown or discussed
- Explicit statements about who products/services are for
- Explicit age references
- Customer/buyer references
- Parent/caregiver references
- Lifestyle or life-stage references
- Any other information contained in the supplied Instagram evidence

Consider the overall repeated pattern of evidence rather than relying on a single post.

--------------------------------------------------
GENDER EVIDENCE RULES
--------------------------------------------------

- Do not determine gender from the brand name.
- Do not assume beauty, fashion, fitness, health, lifestyle, home, or similar categories are automatically male or female.
- Product audience gender must be based primarily on the actual product/service and its intended user or recipient.
- Target audience gender must be based primarily on marketing language, positioning, promotions, creative choices, purchasing context, and who the brand appears to be trying to reach.
- A brand can have a female target audience while its products are for both genders.
- A brand can have a male target audience while its products are for both genders.
- A brand can market to one gender while the product is intended for another gender.
- Do not use stereotypes.
- Do not infer gender solely from models or people appearing in photographs.
- A model's gender is evidence about the creative content, but is NOT by itself proof of the target audience or product audience.
- Consider repeated evidence across the available content.

--------------------------------------------------
TARGET AGE VS PRODUCT AGE
--------------------------------------------------

Keep these completely separate.

TARGET AUDIENCE AGE answers:

"How old are the people this brand's marketing is trying to reach?"

PRODUCT AUDIENCE AGE answers:

"How old are the people who actually use, receive, or are served by this product/service?"

For example:

A children's clothing brand:

- Product audience age = approximately 0-7
- Target audience = parents/caregivers
- Target audience age = ONLY estimate if the marketing provides evidence about the parents'/caregivers' age

Do NOT convert:

"product is for children"

into:

"target audience is 25-35"

unless there is actual evidence supporting that target age.

Likewise, do not convert:

"marketing targets parents"

into a specific parent age range without supporting evidence.

--------------------------------------------------
CONFIDENCE
--------------------------------------------------

Return a separate confidence score from 0 to 100 for ALL FOUR audience dimensions:

- target audience gender
- target audience age range
- product audience gender
- product audience age range

Confidence measures how certain the available evidence supports the classification or estimate.

Confidence scale:

- 90-100 = very strong and explicit evidence
- 75-89 = strong evidence
- 60-74 = moderate evidence
- 0-59 = weak, ambiguous, or limited evidence

A high confidence score does NOT mean the brand strongly targets that gender or age group.

It means the available evidence strongly supports the classification or estimated range.

For age confidence:
- Confidence reflects how strongly the evidence supports the estimated minimum and maximum as a reasonable range.
- Do not give high confidence merely because the product category has a commonly assumed demographic.
- If the age range is mostly an inference rather than directly supported by evidence, use a lower confidence score.
- If both age endpoints are null, age confidence should be 0.
- If only one endpoint is supported, confidence should reflect the limited evidence.

If the evidence is insufficient:

- Use null for the affected gender field.
- Use null for unsupported age endpoints.
- Lower the corresponding confidence score.
- Never invent a value just to avoid returning null.

--------------------------------------------------
OUTPUT FORMAT
--------------------------------------------------

Return ONLY valid JSON.

{
   "target_audience_gender": "male|female|both|null",
   "target_audience_gender_confidence": 0,

   "target_audience_min_age": null,
   "target_audience_max_age": null,
   "target_audience_age_confidence": 0,

   "product_audience_gender": "male|female|both|null",
   "product_audience_gender_confidence": 0,

   "product_audience_min_age": null,
   "product_audience_max_age": null,
   "product_audience_age_confidence": 0,

   "audience_analysis_explanation": "Short explanation distinguishing the marketing target from the product user and explaining the evidence for the gender and age estimates."
}

Keep "audience_analysis_explanation" under 50 words.

The explanation should clearly distinguish:
1. Who the marketing targets.
2. Who the product/service is actually for.
3. The main evidence supporting the age estimates.
4. Any important uncertainty when an age estimate is weak.

Brand name:
{brand_name}

Instagram bio:
{bio}

Business category:
{business_category_name}

Instagram posts:
{posts}

Each post may contain:
- caption
- hashtags
- coauthor_producers: Instagram usernames of creators who co-authored the post with the brand
- mentions: Instagram usernames @-mentioned in the post
- sponsors: Instagram usernames listed in the post's paid-partnership label
- tagged_users: Instagram usernames tagged in the post

Reverse engineering partner creators (creators with a confirmed paid partnership post for this brand; each has creator_username and creator_name):
{re_creators}

Analyze the complete available evidence and return ONLY the JSON object.
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

You will receive a JSON list of employees (id, name, job title, and whether Apollo has an email/phone on file).

Your task has TWO separate steps:

1. Assign an independent sponsorship-contact confidence score from 0 to 100 to EVERY candidate.
2. Rank ALL candidates from highest confidence to lowest confidence.

Do not omit any candidate. Every candidate must appear exactly once in the final JSON.

IMPORTANT: A score of 90 or higher means the candidate is a STRONG ENOUGH MATCH to justify paid Apollo enrichment. Therefore, be conservative with 90+ scores.

A candidate should receive 90+ ONLY when their JOB TITLE provides strong evidence that they directly own, manage, negotiate, coordinate, approve, or personally handle creator sponsorships, influencer marketing, creator partnerships, brand partnerships, talent partnerships, artist relations, ambassador programs, affiliate partnerships, or closely related creator-collaboration work.

The title itself must provide positive evidence of this responsibility. Do NOT infer direct creator/sponsorship responsibility merely because the candidate could potentially be involved.

Do NOT give 90+ merely because someone:
- works in marketing
- is senior
- is a VP, CMO, founder, CEO, or executive
- works for a large or relevant brand
- has an email available
- has "partnerships" somewhere in the title when the partnership function is clearly B2B, education, institutional, sales, distribution, technology, or another non-creator function
- works in events without clear creator, influencer, artist, talent, or sponsorship responsibility
- works in product marketing
- works in social media or community management without clear creator/influencer partnership responsibility
- could theoretically influence the decision
- may possibly execute influencer campaigns
- is likely to know who handles sponsorships

Many companies should legitimately have ZERO candidates scoring 90+.

IMPORTANT DISTINCTION:

"Partnerships" by itself does NOT mean creator partnerships.

For example:
- Director of Educational Partnerships and Institutional Sales -> NOT automatically 90+
- Director of Technology Partnerships -> NOT automatically 90+
- Director of Strategic Partnerships -> usually BELOW 90 unless the title clearly indicates creator/brand/talent/influencer/sponsorship responsibility
- Director of Business Development & Partnerships -> usually BELOW 90
- Director of Artist Relations -> potentially 90+ when the role clearly relates to artists, talent, creators, or brand collaborations
- Director of Brand Partnerships -> strong 90+ candidate
- Director of Creator Partnerships -> strong 90+ candidate

Likewise, "marketing" by itself does NOT mean creator sponsorship responsibility.

For example:
- Marketing Director -> usually BELOW 90
- Director of Marketing -> usually BELOW 90
- VP Marketing -> usually BELOW 90
- Product Marketing Director -> usually BELOW 90
- Marketing Specialist -> usually BELOW 90
- Social Media Manager -> usually BELOW 90
- Event Marketing Manager -> usually BELOW 90 unless the title clearly indicates sponsorship, influencer, creator, artist, or talent responsibility

For senior executives such as CEO, Founder, CMO, VP Marketing, or Head of Marketing:
- Score below 90 when their title is general and there is no clear indication that they personally handle creator/influencer partnerships.
- They may score 90+ only when the title itself strongly indicates direct ownership of creator partnerships, influencer marketing, sponsorships, brand partnerships, talent partnerships, or a closely related function.

Strong 90+ title examples include:
- Influencer Marketing Manager
- Influencer Marketing Director
- Head of Influencer Marketing
- VP Influencer Marketing
- Creator Partnerships Manager
- Creator Partnerships Director
- Head of Creator Partnerships
- Brand Partnerships Manager
- Brand Partnerships Director
- Head of Brand Partnerships
- Sponsorship Manager
- Sponsorship Director
- Head of Sponsorships
- Influencer Relations Manager
- Influencer Relations Director
- Creator Relations Manager
- Creator Relations Director
- Talent Partnerships Manager
- Talent Partnerships Director
- Artist Partnerships Manager
- Artist Relations Manager
- Artist Relations Director
- Ambassador Program Manager
- Ambassador Partnerships Manager
- Affiliate Marketing Manager
- Affiliate Partnerships Manager

These examples are not exhaustive. Other titles can receive 90+ when the title clearly indicates substantially similar direct responsibility.

Titles that are generally BELOW 90 unless additional title information clearly indicates direct creator/sponsorship responsibility include:
- Marketing Manager
- Marketing Director
- Director of Marketing
- VP Marketing
- Head of Marketing
- CMO
- Brand Manager
- Brand Marketing Manager
- Social Media Manager
- Community Manager
- Growth Marketing Manager
- Product Marketing
- Product Marketing Manager
- Product Marketing Director
- Demand Generation
- Marketing Operations
- Marketing Analytics
- Communications
- Public Relations
- SEO
- PPC
- Customer Marketing
- Event Marketing
- Event Marketing Manager
- Business Development
- Business Development Manager
- Strategic Partnerships
- Strategic Partnerships Manager
- Institutional Partnerships
- Educational Partnerships
- Sales Partnerships
- Channel Partnerships
- Technology Partnerships

A candidate with a generic title can still be ranked relatively high compared with weaker candidates, but this does NOT mean they should receive 90+.

The goal is NOT to find the highest-ranking employee.

The goal is to find the employee most likely to personally handle a creator sponsorship opportunity from a creator or creator-management perspective.

Use this confidence scale:

- 100: Almost certainly directly responsible for creator partnerships, influencer marketing, sponsorships, talent partnerships, artist partnerships/relations, ambassador programs, affiliate partnerships, or brand partnerships. The title provides extremely strong evidence of direct ownership.
- 95-99: Extremely strong direct match; the title clearly indicates responsibility for creator/influencer/partnership/sponsorship work and is highly likely to be relevant for creator outreach.
- 90-94: Very strong match; the title strongly indicates that the person likely owns, manages, coordinates, negotiates, approves, or responds to creator sponsorship opportunities.
- 75-89: Relevant marketing or partnership stakeholder, but direct creator sponsorship ownership is uncertain. This range is appropriate for strong general marketing/brand/partnership roles without explicit creator or sponsorship responsibility.
- 50-74: General marketing role with limited or indirect relevance to creator sponsorships.
- 25-49: Senior executive or department leader who may influence marketing decisions but does not appear to personally manage creator sponsorship relationships.
- 0-24: Unlikely to be involved in creator sponsorship decisions.

IMPORTANT SCORING RULES:

- Score each candidate independently.
- Base the score primarily on the job title and its implied responsibilities.
- The strength of the title evidence matters more than seniority.
- Direct creator/influencer/sponsorship responsibility should generally outrank generic marketing seniority.
- Do not force any candidate into the 90+ range.
- If nobody is a strong direct match, return ZERO candidates at 90+.
- If several candidates are genuine strong direct matches, they may ALL receive 90+.
- There is NO maximum number of candidates that may receive 90+.
- Do not lower one candidate's score merely because another candidate scored 90+.
- Do not use ranking position to decide the score.
- Do not assign a high score simply because the candidate is the best available option. A candidate can rank #1 while still scoring below 90 if nobody is a strong direct match.
- Do not invent responsibilities that are not supported by the title.
- Avoid speculative reasoning such as "may handle influencer campaigns", "could manage creators", or "likely runs partnerships" when the title does not provide evidence of this.
- When the title is ambiguous, score conservatively.
- A title containing "partnerships" must be interpreted according to the type of partnership specified.
- B2B, institutional, educational, technology, channel, distribution, sales, and business-development partnerships should generally NOT receive 90+ unless the title also clearly indicates creator, influencer, artist, talent, brand, sponsorship, or similar collaboration responsibility.
- Event roles should generally remain below 90 unless the title explicitly indicates sponsorships, talent, artists, influencers, creators, or similar partnership responsibility.
- Product marketing roles should generally remain below 90 unless the title explicitly indicates creator/influencer/sponsorship responsibility.
- has_email=true may be used as a tie-breaker when candidates are otherwise similarly relevant, but it must NEVER by itself increase a candidate into the 90+ range.
- Phone availability must not increase the sponsorship-contact confidence score.
- Do not use company size, company popularity, brand prestige, or perceived sponsorship budget as a substitute for evidence from the candidate's title.
- Every candidate must receive an independent score even if multiple candidates have similar titles.

When writing the reason:
- Keep it short and evidence-based.
- Prefer describing what the title indicates.
- Do not claim responsibilities that the title does not support.
- For a generic marketing candidate, explain that the role is relevant but does not clearly indicate creator/sponsorship ownership.
- For a non-creator partnership role, explicitly distinguish the partnership type when useful.
- For a strong candidate, identify the direct creator/influencer/sponsorship/artist/talent/brand responsibility indicated by the title.

Candidates:
{candidates}

Reply ONLY with this JSON object, ranking EVERY candidate above, best first:

{"picks": [{"id": "...", "confidence_score": 96, "reason": "Direct influencer marketing responsibility"}, ...]}

Every id from the candidate list above MUST appear exactly once in "picks".
Every candidate MUST have a confidence_score from 0 to 100.
Every candidate MUST have a short one-line reason.
Do not include any additional fields or text outside the JSON object.\
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


#  creator_niches.py

CREATOR_NICHE_DESCRIPTION_PROMPT_NAME = "creator_niche_description"
CREATOR_NICHE_DESCRIPTION_DEFAULT_PROMPT = """\
You are analyzing an Instagram creator.

Niche:
{niche}

Bio:
{bio}

Recent captions:
{captions}

Your task:

Determine what this creator primarily creates content about.

Return ONLY valid JSON.

Rules:

- Focus on actual content themes.
- Use the niche as guidance but do not blindly repeat it.
- Use evidence from bio and captions.
- Ignore hashtags that do not represent content themes.
- Ignore sponsorships and brand names unless central to the creator's content.
- Tags should be highly useful for creator-brand matching.
- Prefer concrete topics rather than generic words.
- Generate 5–10 tags.
- Description must be concise.
- Maximum 35 words.
- Third-person style.
- Do not mention follower counts.
- Do not mention engagement.
- Do not mention demographics.
- Do not mention location unless clearly central to the content.

Return:

{{
"description": "short creator summary",
"tags": ["tag1", "tag2", "tag3"]
}}

Examples:

Fitness creator:
{{"description": "Fitness creator focused on strength training, gym workouts, muscle building, and performance-focused health content.", "tags": ["strength training", "gym workouts", "muscle building", "sports nutrition", "fitness motivation"]}}

Beauty creator:
{{"description": "Beauty creator sharing skincare routines, makeup tutorials, cosmetic reviews, and self-care content.", "tags": ["skincare", "makeup", "beauty reviews", "cosmetics", "self care"]}}
"""


#  shopify_detect.py

BRAND_NICHE_TAGS_PROMPT_NAME = "brand_niche_tags"
BRAND_NICHE_TAGS_DEFAULT_PROMPT = """\
You are analyzing a brand's own website description to extract its real brand name, niche category, and specific sub-niche/category tags for a brand-creator sponsorship matching platform.

Brand: {brand_name}
Niche: {niche}
Website description: {description}

If Brand above is "unknown", determine the brand's real, clean company/brand name from the website description — what the business actually calls itself, not a generic phrase pulled from the text. If Brand is already known (not "unknown"), treat it as a likely-correct hint — repeat it back unchanged if the description supports it or doesn't contradict it, but correct it to the accurate name instead if the description clearly shows it's wrong.

If Niche above is "unknown", determine the single best-fit broad niche category for this brand from the description (e.g. "fashion", "beauty", "food_beverage", "tech", "fitness", "home_goods", "pets", "toys", "automotive", "travel"). If Niche is already known (not "unknown"), just repeat that same value back unchanged — do not second-guess it.

Extract specific, concrete sub-niche tags/keywords that describe what this brand actually does or sells, beyond its broad niche category (e.g. for a "fashion" brand: "sustainable clothing", "streetwear", "plus-size fashion"; for a "tech" brand: "smart home devices", "gaming laptops", "wireless earbuds").

Reply ONLY with this JSON object, no extra text:
{"name": "...", "niche": "...", "tags": ["...", "..."]}
Use an empty list for tags if the description doesn't give enough information — do not invent tags not supported by the input. If you truly cannot determine a niche from the description either, use "unknown" for niche. If you truly cannot determine a real brand name from the description, use "unknown" for name.\
"""


#  content_creator_re.py

BRAND_CHECK_PROMPT_NAME = "brand_check"
BRAND_CHECK_DEFAULT_PROMPT = """You are an Instagram sponsorship detection system.

Your task is NOT to identify whether an account is a brand.

Your task is ONLY to identify brands that are very likely **paid sponsors, paid partners, advertisers, affiliate/referral partners, ambassadors, or official commercial collaborators** in THIS specific Instagram post.

**IMPORTANT: Gifted products, free products, PR packages, product seeding, and unsolicited gifts DO NOT count as sponsorships or brand partnerships by themselves.**

For each confirmed brand, also determine whether the creator is using a discount code, referral code, affiliate code, promo code, coupon code, creator code, ambassador code, or "use my code" style offer for that brand in THIS post.

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

1. ONLY return a username if there is strong evidence that the account is a **paid sponsor, paid partner, advertiser, affiliate/referral partner, ambassador partner, official commercial collaborator, or brand being commercially promoted** in THIS specific post.

2. **GIFTED BRANDS MUST NOT BE RETURNED.**

   Do NOT return a brand if the only evidence is that:

   * the product was gifted
   * the creator received a free product
   * the creator was sent a PR package
   * the creator received a complimentary item
   * the creator was sent free merchandise
   * the creator received a product for free
   * the post is part of product seeding
   * the caption says "gifted by"
   * the caption says "PR"
   * the caption says "PR package"
   * the creator thanks the brand for sending a product
   * the creator says the brand sent them something
   * the creator received a product without evidence of payment, affiliate compensation, referral compensation, or a commercial partnership

   **Gifted product ≠ paid sponsorship.**

   For example:

   "Thanks @brand for sending me these shoes!" → DO NOT return @brand.

   "Gifted by @brand" → DO NOT return @brand.

   "PR package from @brand" → DO NOT return @brand.

   Only return the brand if there is additional strong evidence of a **paid, affiliate/referral, ambassador, or other commercial partnership**.

3. A username being a brand account is NOT sufficient.

4. Do NOT return accounts that are merely:

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

5. Do NOT infer sponsorship simply because:

   * the account is tagged
   * the account is mentioned
   * the account is a coauthor
   * the account appears in the photo
   * the creator follows or collaborates with the account

6. If the evidence is ambiguous, uncertain, weak, or missing, return an empty list.

7. Treat false positives as much worse than false negatives.

8. Strong evidence includes:

   * paid partnership marker is true and a referenced account appears to be the partnered brand
   * explicit paid sponsorship language such as:

     * "ad"
     * "#ad"
     * "#sponsored"
     * "#paidpartnership"
     * "paid partnership"
     * "partnered with"
     * "in partnership with"
     * "sponsored by"
     * "ambassador for"
     * "official partner"
     * "brand partner"
     * "working with [brand]" when clearly commercial
     * "campaign with [brand]" when clearly commercial
   * clear commercial promotion of a company's product, service, app, store, brand, or commercial offering
   * creator-specific discount, referral, affiliate, ambassador, creator, or promo code offers that can be confidently linked to a specific referenced brand account
   * affiliate/referral links that clearly generate purchases, signups, downloads, or commissions for the referenced brand

9. Affiliate, referral, ambassador, and creator-code promotions COUNT as brand partnerships.

   If the creator promotes a product, service, app, subscription, store, or commercial offering and provides:

   * a discount code
   * promo code
   * referral code
   * creator code
   * ambassador code
   * affiliate code
   * affiliate link
   * referral link
   * tracked purchase link
   * commission-generating link

   then treat the associated brand as a promotional/partner brand for this post EVEN IF:

   * paid partnership marker is false
   * the creator never says "sponsored"
   * the creator never says "paid partnership"

   provided there is strong evidence connecting the promotion to a specific referenced brand account.

10. Discount codes alone are NOT sufficient.

Do NOT return a brand merely because a code appears in the caption.

Return a brand only when:

* a specific referenced account is clearly associated with the promoted product/service, AND
* the code, link, or promotion appears intended to drive purchases, signups, downloads, or sales for that brand.

11. **Do not confuse a normal product recommendation with a sponsorship.**

A creator mentioning, reviewing, liking, using, wearing, or recommending a brand's product does NOT automatically mean the brand is a sponsor.

Organic product use or recommendation without evidence of a commercial relationship should NOT be returned.

12. **Gifted products must be treated as non-sponsored unless there is additional commercial evidence.**

If the post contains both:

* evidence that the product was gifted, AND
* separate evidence of a paid partnership, affiliate/referral relationship, ambassador relationship, or creator-specific commercial promotion,

then the brand MAY be returned because of the commercial relationship.

However, the gifted-product evidence itself must NEVER be the reason for returning the brand.

13. If multiple brands appear, only return those that are clearly being:

* paid for
* commercially partnered with
* advertised
* sponsored
* promoted through an affiliate/referral relationship
* promoted through an ambassador/creator-code relationship
* officially commercially collaborated with

14. If you cannot confidently conclude that a referenced account is a sponsor/brand partner for THIS post, return an empty list.

15. Brands essentially never share a single sponsored/paid-partnership post with each other or with a long list of other tagged accounts. If the post references more than a small handful of accounts in total (mentions + tagged_users + coauthor_producers combined), treat that as a strong signal this is a giveaway, brand round-up, general shoutout, event, community post, creator-network post, or participant list rather than a genuine brand partnership.

In such cases, return an empty list unless the evidence for one specific brand is overwhelming.

16. The returned usernames MUST come from the referenced accounts provided in mentions, tagged_users, or coauthor_producers.

Never invent usernames.
Never infer usernames that are not explicitly present in the provided account lists.

17. Focus on THIS specific post only.

Do not infer sponsorship based on:

* prior creator-brand relationships
* assumptions about the creator
* assumptions about the account
* historical partnerships
* outside knowledge

18. Set has_referral_code=true ONLY when the caption/post clearly contains a creator-specific discount, referral, affiliate, ambassador, creator, or promo code offer for that brand.

A generic sale announcement, seasonal promotion, brand-wide discount, coupon campaign, "shop now" CTA, or ordinary sponsorship is NOT enough.

19. If there is a visible creator-specific code, put the exact code text in referral_code.

Examples:

* "Use code ALI10" → "ALI10"
* "Use creator code ALI" → "ALI"

If referral/discount-code evidence exists but no exact code text is visible, use:

* has_referral_code = true
* referral_code = null

20. **Priority rule for classification:**

When deciding whether to return a brand, use this hierarchy:

**RETURN:**

* Paid partnership
* Paid sponsorship
* Affiliate partnership
* Referral partnership
* Ambassador partnership
* Creator-code partnership
* Commercial campaign partnership
* Clearly paid/commercial brand promotion

**DO NOT RETURN:**

* Gifted product only
* Free product only
* PR package only
* Product seeding
* Unsolicited product
* Organic product recommendation
* Normal brand mention
* Brand tag without commercial evidence
* Brand coauthor without commercial evidence
* Product appearance without commercial evidence

21. When there is a conflict between "gifted" evidence and sponsorship evidence, only return the brand if there is **separate positive evidence of a paid or commercially compensated relationship**.

22. When in doubt, return an empty list.

Return ONLY valid JSON:

{
  "brands": [
    {
      "username": "username1",
      "has_referral_code": true,
      "referral_code": "CODE10"
    },
    {
      "username": "username2",
      "has_referral_code": false,
      "referral_code": null
    }
  ]
}
"""


#  pitching.py

PITCH_PROMPT_NAME = "brand_pitch_generation"
PITCH_DEFAULT_PROMPT = """\
You are writing a short outreach email FROM a content creator TO a brand, pitching a sponsorship/partnership. Write it in the creator's own voice, as if they wrote it themselves.

Creator:
Name: {creator_name}
Handle: {creator_handle}
Niche: {creator_niche}
Follower count: {creator_follower_count}

Brand:
Name: {brand_name}
Niche: {brand_niche}
Description: {brand_description}

Contact you're writing to: {contact_name} ({contact_title})

What the creator wants to say:
Their story / why this brand: {story}
Product or line they're interested in: {product_reference}
Past brand partnerships worth mentioning: {past_brand_partnership}
Link to relevant content: {content_link}

RULES

1. Write ONLY the email body — no subject line, no "Subject:", no placeholders like [Name].
2. Address the contact by first name if one was given; otherwise open naturally without a name.
3. Sound like a real person emailing another real person — warm, specific, genuinely interested in the brand, not salesy or generic.
4. Do NOT use em dashes anywhere. Use commas, periods, or separate sentences instead.
5. Do NOT use AI-sounding phrases like "I hope this email finds you well", "I'm reaching out because", "in today's digital landscape", "I wanted to touch base", or similar stock filler.
6. Weave in the creator's story and why THIS brand specifically — avoid generic praise that could apply to any brand.
7. Mention the product/line and past partnerships only if they were actually given above — do not invent details.
8. Include the content link naturally if one was given.
9. End with a soft, low-pressure call to action (e.g. suggesting a quick chat), not a hard ask.
10. Keep it tight — a few short paragraphs, not a wall of text.

Reply with the email body only, plain text, no markdown formatting.\
"""


#  rate_intelligence.py

RATE_INTEL_PROMPT_NAME = "rate_intelligence_estimate"
RATE_INTEL_DEFAULT_PROMPT = """\
You are a creator-economy rate consultant, estimating a fair market rate range for a sponsorship deliverable.

Creator:
Tier: {creator_tier}
Follower count: {creator_follower_count}
Primary platform: {creator_primary_platform}

Brand:
Name: {brand_name}
Sponsorship activity score (0-100): {sponsorship_activity_score}
Meta ads active: {meta_ads_active}

Deal being estimated:
Platform: {platform}
Deliverable type: {deliverable_type}
Exclusivity: {exclusivity}
Usage rights: {usage}
Duration (months): {duration_months}

Estimate a realistic USD rate range for this specific deliverable, given the creator's tier/following and the brand's apparent sponsorship budget/activity level. Wider exclusivity, broader usage rights, and longer durations should push the range higher.

Reply ONLY with this JSON object, no extra text:
{"rate_min": 0, "rate_max": 0, "currency": "USD", "reasoning": "..."}
rate_min and rate_max must be plain integers (whole dollars, no commas or symbols). Keep reasoning to 2-3 sentences explaining the key factors.\
"""


#  contract_advice.py

CONTRACT_ADVICE_PROMPT_NAME = "contract_advice_review"
CONTRACT_ADVICE_DEFAULT_PROMPT = """\
You are a contract-review assistant for content creators, helping them spot red flags in brand-sponsorship agreements before signing. You are not a lawyer and this is not legal advice — flag concerns in plain language a creator can understand.

Contract text:
{contract_text}

Review this contract for common creator-sponsorship red flags, including but not limited to:
- Exclusivity terms that are broader or longer than the payment justifies
- Vague or open-ended usage rights (e.g. "in perpetuity", "any media now known or later invented") without extra compensation
- Missing or unclear payment terms/timeline
- No kill fee or cancellation terms
- Automatic renewal clauses
- Unreasonable revision/approval cycles
- Ownership of content/IP being fully assigned away
- Missing FTC disclosure requirements

Reply ONLY with this JSON object, no extra text:
{"looks_good": true, "issues": ["...", "..."], "summary": "..."}
Set looks_good to false if there is at least one real concern worth flagging. issues should be short, specific, plain-language bullet points (empty list if none found). summary should be 2-3 sentences giving the creator an overall read.\
"""


#  brand_instagram_profile.py

LINK_CLASSIFY_PROMPT_NAME = "instagram_link_classify"
LINK_CLASSIFY_DEFAULT_PROMPT = """\
You are classifying a URL found in an Instagram bio to determine what kind of link it is, and — only if it turns out to be the brand's own official website — extracting the brand's real, clean name.

Instagram handle: {handle}
Instagram display name (from Instagram, often messy marketing copy): {full_name}
Bio: {bio}
URL being classified: {url}
Brand's already-known real name (if any, e.g. from Wikidata — a likely-correct hint to confirm or correct, not a fact to blindly repeat): {known_name}
Brand's already-saved website (if any, e.g. from Wikidata — may already be correct, outdated, or wrong): {existing_website}

Classify this URL into exactly one category:
1. "website" — the brand's own official website/domain (online store, company site, product page hosted on the BRAND'S OWN domain) — not a social platform, link-aggregator tool, or third-party marketplace
2. "social" — a profile on another social media platform (Facebook, TikTok, YouTube, Twitter/X, Pinterest, Threads, WhatsApp, Discord, etc.), or Instagram itself
3. "linktree" — a link-in-bio aggregator page (e.g. Linktree, Beacons, Milkshake, Later, Campsite, Lnk.bio, Direct.me, and similar tools) that itself contains a list of other links
4. "marketplace" — a listing, storefront, or store page for this brand hosted on a third-party marketplace/retail platform (Amazon — including Amazon Storefronts like "amazon.com/stores/page/...", Etsy, eBay, Walmart, AliExpress, Shopee, Temu, etc.). This is NOT the brand's own website, even if it's a dedicated branded page on that platform.
5. "unknown" — cannot confidently tell from the URL/bio alone

Only classify as "website" if you are genuinely confident this specific URL is the brand's own official site — a URL that merely mentions or is related to the brand (a fan page, a syndicated listing, an unofficial reseller) is NOT "website" even if none of the other categories fit well either. When unsure, use "unknown" rather than guessing "website" — reporting no website found is the correct outcome far more often than a confident-sounding wrong classification.

Judge "website" by the DOMAIN, not the specific path or query string — a URL like "https://brand.com/register?ref=800000016" or "https://brand.com/shop/product123" is still the brand's own website (category "website"); a tracking parameter, referral code, or deep link does not make it a linktree or unknown. But a URL whose domain is a third-party marketplace (amazon.com, etsy.com, ebay.com, walmart.com, etc.) is ALWAYS "marketplace", never "website" — no matter how specific or branded-looking the path is (e.g. "amazon.com/stores/page/8A8B3EB2-E356-4C27-B4B2-12EEFCEB05CF" is "marketplace", not "website"). Only the domain root is kept once classified as "website", so don't let the path/query change your answer for a genuine brand domain.

Use the "already-known" fields above as context, not a shortcut:
- If this URL's domain matches the already-saved website, that's a strong confirming signal this is genuinely "website" (not proof by itself — still judge the URL/bio on their own merits).
- If it differs from the already-saved website, that does NOT automatically make this URL wrong, nor does it mean the already-saved one was wrong — a brand can own more than one domain, and a previously saved website can itself be outdated or incorrect. Classify this URL on its own evidence.

If, and only if, category is "website": also give the brand's real, clean name in the "name" field. The Instagram display name above is often marketing copy, not the real name — it can include emojis, taglines, "Official", "| Shop Now", pipe-separated slogans, or ALL CAPS styling. Derive the actual brand name from the display name, bio, and this website's own domain/identity together (e.g. domain "jpfans.com" supports a name like "JPfans"), preserving deliberate stylization (e.g. "adidas" lowercase, "eBay"). If an already-known real name was given above, treat it as a strong hint — repeat it back unchanged if this website supports it or doesn't contradict it, correct it only if this website's own evidence clearly shows it's wrong.

If category is NOT "website" (social/linktree/marketplace/unknown), "name" MUST be an empty string — do not guess a name for a link that isn't the brand's own site.

Reply ONLY with this JSON object, no extra text:
{"category": "website", "name": "", "reason": "short one-line reason"}\
"""

WEBSITE_PICK_PROMPT_NAME = "brand_website_search_pick"
WEBSITE_PICK_DEFAULT_PROMPT = """\
You are identifying a brand's real official website from search results, and confirming or correcting its name.

Instagram handle: {handle}
Instagram bio: {bio}
Instagram external URL (if any): {external_url}
Currently saved brand name (if any): {saved_name}
Currently saved website for this brand (if any, e.g. from Wikidata — may already be correct, outdated, or wrong): {existing_website}

Search results for "{query}" (already deduplicated to one representative URL per domain — social/platform domains like Instagram, YouTube, Linktree, etc. have already been removed, and each is annotated with how many times that domain appeared across the full raw result set):
{results}

Decide which ONE of the results above (if any) is most likely the brand's own official website — not a marketplace listing (Amazon, Etsy shop page, etc.), not a press/news article, not an unrelated business that happens to share a similar name. A domain appearing many times across the raw results is a meaningful signal it's the real site (its own multiple pages tend to all get indexed), but isn't decisive on its own — still weigh it against the title/snippet content and the handle/bio/saved-name context.

Judge by the DOMAIN of each result, not its specific path or query string — a result URL like "https://brand.com/register?ref=800000016" is still a valid pick if brand.com is genuinely the brand's own domain; a tracking parameter or deep link doesn't disqualify it. Only the domain root is kept once picked.

IMPORTANT — do not pick just because it's the best of a mediocre set. Being the least-bad option among the candidates is NOT the same as being confidently the brand's own official website. A personal blog about the brand, a fan-run page, a syndicated directory listing, an app landing page, or an unofficial reseller are all still NOT the official website, even if nothing better is on the list and even if they're clearly related to the brand. If you are not genuinely confident any single result is the real official site, set "confident" to false and index to 0 — reporting no website found is the correct answer far more often than guessing wrong, and is strongly preferred over a confident-sounding wrong pick.

Weighing the currently saved website (if one is given above): if its domain also appears among the candidates and nothing else looks more clearly correct, that agreement is a good reason to confidently pick it. If NONE of the candidates is confidently the real official site, and a currently saved website was already given, lean toward "confident": false rather than replacing a plausibly-correct existing website with a weaker guess — a wrong replacement is worse than leaving a decent existing value alone. Only pick a result on a DIFFERENT domain than the currently saved website when the evidence genuinely shows that different domain is correct (not merely different).

If, and only if, "confident" is true (which requires index to not be 0): also give the brand's real, clean name in the "name" field, using the search result titles/snippets together with the saved name. If "Currently saved brand name" above is "unknown" or empty, determine the name from the search results. If a saved name IS given, treat it as a likely-correct hint — repeat it back unchanged if the results support it or don't contradict it, but correct it to the accurate name instead if the results clearly show the saved name is wrong.

If "confident" is false, index MUST be 0 and "name" MUST be an empty string.

Reply ONLY with this JSON object, no extra text:
{"index": 0, "confident": false, "name": "", "reason": "short one-line reason"}
Use the 1-based index of the correct result from the numbered list above only when confident is true; use index 0 and confident: false whenever you are not genuinely sure any result is the brand's real official website.\
"""


#  pipeline/enrichment/linkedin_verify.py

LINKEDIN_COMPANY_MATCH_PROMPT_NAME = "linkedin_company_match"
LINKEDIN_COMPANY_MATCH_DEFAULT_PROMPT = """\
You are checking whether a person's current employer, as shown on their public LinkedIn profile, is still the same company as a specific brand — to confirm whether a previously-found contact at that brand is still there.

Brand name: {brand_name}
Brand domain (if known): {brand_domain}

LinkedIn profile's current employer: {current_company}
LinkedIn current-employer company page URL (if any): {current_company_url}

Decide whether the LinkedIn current employer is the SAME real-world company as the brand — allowing for naming differences that don't change the underlying company (legal suffixes like "Inc"/"LLC"/"Ltd"/"Co", punctuation, capitalization, a parent/holding company name being used interchangeably with a well-known sub-brand it's known to own, regional divisions of the same company). Do NOT match a different, unrelated company just because it's in the same industry or has a superficially similar name (e.g. "Nike" is not "Nike Foundation" is not "Nike Golf" unless you're confident those are genuinely the same operating entity being pitched).

If the current employer is missing/empty, that means LinkedIn doesn't show one (private, unemployed, hidden) — answer "match": false, since there's nothing to confirm they're still there.

Reply ONLY with this JSON object, no extra text:
{"match": false, "reason": "short one-line reason"}
"""


#  matching/llm_ranking_v2.py  (Matches v2)
LLM_BRAND_RANKING_PROMPT_NAME = "creator_brand_llm_ranking"

LLM_BRAND_RANKING_DEFAULT_PROMPT = """
You are matching a content creator with brands for realistic paid sponsorship opportunities on a creator-brand matching platform.

CREATOR
Niche(s): {creator_niches}
Sub-niche tags: {creator_sub_niches}
Content tags: {content_tags}
Description: {creator_description}
Profile summary: {embedding_text}

BRANDS (JSON list — each has id, name, niche, description, tags):
{brands_json}

For EVERY brand, estimate how relevant and realistic a paid sponsorship would be for THIS creator. Return a confidence score from 0 to 100.

IMPORTANT MATCHING PRINCIPLE

Do NOT assume that two entities are a strong match just because they share the same broad niche.

First determine whether the brand is:

1. BROAD / GENERAL:
   Its products can naturally be used, promoted, or recommended by many creator types within the niche.

2. SPECIALIZED:
   Its products are mainly relevant to a specific activity, profession, content type, audience, body concern, instrument, sport, or sub-niche.

Broad brands may receive good scores for creators in the same niche even when the creator does not mention the exact product category, as long as using the product would be natural.

Specialized brands should receive high scores ONLY when the creator's description, tags, sub-niche, or content clearly indicate that specialization.

SPECIALIZED MISMATCH RULE (overrides every other rule and the scoring bands below):
If a brand is SPECIALIZED and the creator's description, tags, sub-niches and content do NOT show that specialization, its confidence MUST be 15 or lower.
This applies when the creator's content is general for the niche, or is about a different specialization.
Examples:
* Drum / percussion brands for a singer, songwriter or music producer whose content does not mention drums or percussion -> 15 or lower. Only drummers, percussionists, or creators whose content clearly shows drum work should see drum brands above 15.
* Vinyl pressing, vinyl retailers, record labels or distribution services for a creator who does not say they release physical music or work with labels -> 15 or lower. Posting songs, covers or original music online does NOT count as releasing physical music.
* Powerlifting, cycling or other discipline-specific gear for a Fitness creator who does not do that discipline -> 15 or lower.
* Products for a specific hair type, skin concern or medical condition for a creator who does not cover it -> 15 or lower.
These brands should end up at the bottom of the ranking, below every broad or matching brand.

Do not force exact tag-to-tag matching. Judge whether the brand's product is realistically useful or promotable by this type of creator.

MUSIC EXAMPLES

* A singer, songwriter, musician, or music producer can reasonably match with broad music products such as microphones, headphones, audio interfaces, studio equipment, music software, instruments, recording equipment, or other generally useful music products.

* A guitar brand may still be relevant to a singer, songwriter, producer, or general musician when guitar/instrument use would be natural, even if the creator does not explicitly have the tag "guitar".

* A highly specialized drum/percussion brand should rank high primarily for drummers, percussionists, producers who clearly work with drums/percussion, or creators whose content indicates that use case.

* A vinyl record, turntable, DJ-specific, orchestral-instrument, or other specialized brand should not receive a high score merely because the creator's niche is Music. Look for supporting evidence that the creator's content makes that product naturally relevant.

Apply the same logic to all supported niches:

HEALTH
A general wellness, nutrition, or health-focused brand may fit many Health creators.
A specialized product such as a pregnancy product, dental device, glucose monitor, or condition-specific product requires relevant creator content or audience evidence.

FITNESS
General activewear, recovery, hydration, or broad fitness products may fit many Fitness creators.
Specialized products for powerlifting, running, cycling, bodybuilding, yoga, or another specific discipline should score highest when the creator actually participates in or discusses that discipline.

BEAUTY
General skincare, makeup, haircare, or beauty products may fit many Beauty creators.
Highly specialized products for a particular hair type, skin concern, nail technique, professional procedure, or other narrow use case require supporting creator evidence.

SCORING

90-100:
Exceptional and highly natural sponsorship fit.
The brand directly matches the creator's content, specialization, audience, or demonstrated use case.

80-89:
Strong fit.
The brand is highly relevant and the creator could promote it naturally, even if there is not an exact tag match.

65-79:
Good broader-niche fit.
There is a realistic sponsorship opportunity, but the connection is broader or less specific.

45-64:
Partial or indirect fit.
There is some plausible overlap, but the product is not strongly connected to the creator's demonstrated content.

20-44:
Weak fit.
The brand shares some broad niche relationship, but its product or specialization is unlikely to be naturally promoted by this creator.

0-19:
No meaningful or realistic sponsorship fit.

RANKING RULES

* Prioritize semantic and real-world product relevance over exact keyword overlap.
* Exact matching specialized tags are strong positive evidence.
* Missing an exact tag is NOT automatically negative when the brand is broadly useful within the creator's niche.
* A shared broad niche alone is NOT enough to justify a high score for a specialized brand.
* Do not invent creator skills, interests, demographics, or activities that are not supported by the provided information.
* Do not reward brands for being large, famous, or recognizable.
* If a brand has very little information, score cautiously.
* Compare brands against each other so the most naturally relevant sponsorship opportunities receive the highest scores.
* Specialized brands with no creator-specific evidence should rank below broad brands that are naturally usable by the creator.
* Apply the SPECIALIZED MISMATCH RULE last: a specialized brand the creator's content does not support is capped at 15.

Judge only from the information provided.

Reply ONLY with this JSON object, with no extra text:
{"rankings": [{"id": 123, "confidence": 0, "reason": "one short sentence explaining why this brand is or is not a natural sponsorship fit"}]}

Include every brand id from the input list exactly once.
"""


#  matching/llm_ranking_v3.py  (Matches v3)

LLM_BRAND_RANKING_V3_PROMPT_NAME = "creator_brand_llm_ranking_v3"

LLM_BRAND_RANKING_V3_DEFAULT_PROMPT = """\
You are ranking brands for a content creator on a creator-brand sponsorship matching platform.

CREATOR
Niche(s): {creator_niches}
Sub-niche tags: {creator_sub_niches}
Content tags: {content_tags}
Description: {creator_description}
Profile summary: {embedding_text}
Gender: {creator_gender}

BRANDS (JSON list - each has id, name, niche, description, tags, why_it_matches, target_audience_gender, product_audience_gender):
{brands_json}

"why_it_matches" lists the platform's verified match signals between that brand and this creator. They are written to the creator, so "you" / "yours" means THIS creator. Examples: recent paid partnerships, partnerships with creators whose content is similar to this creator's, follower-size fit, a verified partnerships contact, same niche, the brand sponsoring creators in this creator's niche, audience fit.

For EVERY brand, score two parts separately from 0 to 100, then combine them. CONTENT FIT counts for 65% and MATCH EVIDENCE for 35%.

1. CONTENT FIT (65%) - how naturally the brand's products fit the creator's niche, sub-niches, tags and description.
   - BROAD brands (products many creators in the niche can naturally use or recommend) can fit well even without an exact tag match.
     e.g. a singer, songwriter or music producer naturally fits microphones, headphones, audio interfaces, studio gear, music software and general instruments such as guitars.
   - SPECIALIZED brands (a specific instrument, sport, discipline, body/skin/hair concern, medical condition, or a service aimed at artists such as vinyl pressing, record labels, distribution or mastering) fit well ONLY when the creator's information shows that specialization.
     e.g. drum/percussion brands fit drummers, not singers; vinyl pressing fits creators who release physical music; a powerlifting brand fits powerlifters, not yoga creators; a curly-hair brand fits creators who cover curly hair.
   - Apply the same logic to Music, Health, Fitness and Beauty.
   - GENDER FIT is part of content fit. Use the creator's Gender and each brand's target_audience_gender (who its marketing targets) and product_audience_gender (who its products are made for). It is a SOFT signal, never an automatic exclusion:
     - If the creator's gender is "not specified", ignore gender completely.
     - "unknown" means the brand's audience gender has not been determined - treat it as neutral, never as a mismatch.
     - target or product audience "both", or the creator's own gender: no gender penalty.
     - Brand targets the OPPOSITE gender: do NOT exclude it. Many such brands still sponsor creators of the other gender and their products can be used or promoted by them (e.g. a female-targeted skincare, fragrance, haircare, wellness, food, home or lifestyle brand for a male creator; a female-targeted brand whose product_audience_gender is "both"). Lower content fit only a little in that case.
       Positive evidence of fit for this creator overrides the audience label: product_audience_gender "both", or why_it_matches lines showing it has backed creators of this creator's gender ("has already backed ... male creators", "partnered with creators ... same as yours").
     - Lower content fit strongly (into the 0-19 band) ONLY when the product itself is clearly made for and only usable by the opposite gender (e.g. menstrual or maternity products, bras or women's intimate apparel, men's beard care) AND there is no evidence the brand works with creators of this creator's gender.
   Content fit scale:
   - 90-100: direct, natural fit; the brand's specialization matches the creator's content.
   - 70-89: strong fit; a broad brand the creator could naturally promote.
   - 45-69: partial or indirect fit.
   - 20-44: weak fit; shares only a broad niche.
   - 0-19: no meaningful fit, or a SPECIALIZED brand the creator's content does not support (see the mismatch rule below).

2. MATCH EVIDENCE (35%) - how strong the brand's why_it_matches signals are.
   - Strongest: a recent paid partnership (especially in the last month or 3 months, or with creators whose content matches this creator's); partnerships with creators similar to this creator; follower/size fit; the brand sponsoring creators in this creator's niche.
   - Helpful: a verified partnerships contact; same niche; YouTube sponsorships; a smaller or growing brand (easier to reach); tag overlap with the creator's content; a latest product to pitch around.
   - Weak: lines that only describe the brand's audience (target audience gender or age, who its products are made for) - count them very little as evidence (gender fit is judged under CONTENT FIT).
   Match evidence scale:
   - 90-100: a recent paid partnership plus other strong signals.
   - 70-89: a recent paid partnership, or several strong signals.
   - 40-69: some helpful signals but no recent paid partnership.
   - 10-39: only weak signals.
   - 0-9: no signals (empty why_it_matches).

Final confidence = round(0.65 x content fit + 0.35 x match evidence).
Examples: fit 90 and evidence 80 -> 87; fit 90 and evidence 0 -> 59; fit 40 and evidence 90 -> 58; fit 10 and evidence 90 -> 38.

SPECIALIZED MISMATCH RULE (apply AFTER the formula; overrides it):
If a brand is SPECIALIZED and the creator's description, tags, sub-niches and content do NOT show that specialization, the final confidence MUST be 15 or lower - no matter how strong its match evidence is.
This applies when the creator's content is general for the niche, or is about a different specialization.
Examples:
- Drum / percussion brands for a singer, songwriter or music producer whose content does not mention drums or percussion -> 15 or lower, even with a recent paid partnership. Only drummers, percussionists, or creators whose content clearly shows drum work should see drum brands above 15.
- Vinyl pressing, vinyl retailers, record labels or distribution services for a creator who does not say they release physical music or work with labels -> 15 or lower. Posting songs, covers or original music online does NOT count as releasing physical music.
- Powerlifting, cycling or other discipline-specific gear for a Fitness creator who does not do that discipline -> 15 or lower.
- Products for a specific hair type, skin concern or medical condition for a creator who does not cover it -> 15 or lower.
These brands should end up at the bottom of the ranking, below every broad or matching brand.

RULES
- Score each brand on the absolute scale above, independently of the other brands in this list. Brands are sent in separate batches, so never rescale or spread scores across the list.
- Do not invent creator skills, interests, activities or demographics that the creator information does not support (for example, do not assume the creator releases physical music, tours, runs a label, or practices a specific sport or discipline unless stated).
- Do not reward a brand for being large, famous or recognizable.
- If a brand has very little information, score cautiously.
- Use only the information provided.

Reply ONLY with this JSON object, no extra text:
{"rankings": [{"id": 123, "confidence": 0, "reason": "one short sentence naming the content fit and the strongest match signal"}]}
Include every brand id from the list exactly once.
"""
