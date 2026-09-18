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
