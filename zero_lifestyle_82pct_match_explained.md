# How Zero Lifestyle's match score was calculated

> Updated after the instagram_post_score/youtube_post_score formula change:
> the score is now the matched recency window's weight only (not
> `count × weight`). This moved the match from 82% to **79%**.

Creator: id=1 (niche `fashion`, tier `macro`, primary_platform `instagram`,
follower_count 107,000, instagram_followers 107,000, audience gender 60%
male / 40% female, audience age 25–35 / bracket `29_35`, top audience
country United States 100%)

Brand: Zero Lifestyle (niche `fashion`, operating in Pakistan, typical
creator tier `macro`, avg IG collaborator followers 546,474, audience
gender 73.9% male / 26.1% female, audience age groups `{teen: 1.9%, adult:
5.7%, young_adult: 92.5%}`, top countries Pakistan 72.8% / India 8.6% /
United States 4.9% / Japan 2.5% / Bangladesh 2.5%, meta_ads_active=true,
meta_ads_recency_days=32, meta_ads_no_end_date=true, meta_ads_count=25)

All numbers below are pulled live from the real database rows for this
exact pair — nothing is illustrative/rounded-for-example.


## Step 0 — Hard filters (pipeline/matching/matcher.py)

Zero Lifestyle passed every hard filter, so it's in the candidate pool at
all: has a brand_match_profile + embedding, sponsorship activity not
confirmed-zero, no excluded-category niche match, Instagram-specific
checks pass (primary_platform=instagram, has_instagram not false,
insta_lowest/insta_highest present, creator's follower_count 107,000 within
Zero Lifestyle's confirmed collaborator range ±30%), and at least one exact
niche match (`fashion` == `fashion`).

Hard filters only decide IN/OUT — they don't contribute to the score
itself.


## Step 1 — Score each of the 7 dimensions (pipeline/matching/scoring.py)

### 1. niche_match — weight 0.25 — score 1.0
Exact case-insensitive match: creator's `fashion` == brand's `fashion` →
niche_compatibility() returns 1.0 immediately (see niche_compatibility.py).

### 2. sponsorship_activity — weight 0.20 — score 0.8525
Computed live in `_score_sponsorship_activity()`, summed then clamped to
[0.0, 1.0]:

  a) Meta ads component (max 0.6):
     - meta_ads_active = true            → +0.1
     - meta_ads_recency_days = 32        → (1/32) × 0.3 = +0.009375
     - meta_ads_no_end_date = true       → +0.1
     - meta_ads_count = 25 > 5           → +0.1
     subtotal = **0.309375**

  b) youtube_post_score: most recent YouTube sponsorship video for this
     brand is 233 days old — outside every recency window (7/14/30/90/180
     days, checked in that order) → **0.0**

  c) instagram_post_score: 14 Instagram posts carry a paid_partnership/
     sponsors/tagged_users/coauthor_producers signal; the MOST RECENT one
     is 35 days old. The 7/14/30-day windows don't contain it; the 90-day
     window is the first one that does. The score is simply that window's
     weight — **not** multiplied by the 14 posts found:
     instagram_post_score = **0.3**

  d) instagram_tier_score (brand avg IG collaborator followers vs
     creator's own instagram_followers):
     diff% = |546,474 − 107,000| / (546,474 + 107,000/2) × 100
           = 439,474 / 600,024 × 100 ≈ 73.24%
     score = (1 / 73.24) × 2 ≈ **0.0273**

  e) Gender diff scores (same diff%→score formula, per gender):
     male:   diff% = |0.739 − 0.6| / (0.739 + 0.6/2) × 100 = 0.139/1.039×100 ≈ 13.38%  → score = (1/13.38)×2 ≈ **0.1495**
     female: diff% = |0.261 − 0.4| / (0.261 + 0.4/2) × 100 = 0.139/0.461×100 ≈ 30.15%  → score = (1/30.15)×2 ≈ **0.0663**

  f) Age-group hop score: brand's dominant age bucket is `young_adult`
     (old taxonomy — 92.5% of its audience), creator's bracket is `29_35`
     (new taxonomy). These label sets don't overlap, so the hop lookup
     fails → this component contributes **None** (skipped, not added).

  RAW total = 0.309375 + 0.0 + 0.3 + 0.0273 + 0.1495 + 0.0663 ≈ **0.8525**
  Already inside [0.0, 1.0], so the clamp changes nothing this time →
  **sponsorship_activity score = 0.8525**

### 3. audience_demographics — weight 0.20 — score 0.455
Average of whichever of these 3 sub-scores are available (age is skipped
here too, same old/new taxonomy mismatch as above):
  - gender:  1 − |0.6 − 0.739| = 1 − 0.139 = **0.861**
  - age:     brand's `{teen, adult, young_adult}` buckets don't match
             `_AGE_BUCKET_RANGES`'s `12_16...60_plus` keys → **skipped (None)**
  - country: creator's audience is 100% "united states"; brand's audience
             includes "united states" at 4.9% → overlap = min(1.0, 0.049) = **0.049**
             (Pakistan/India/Japan/Bangladesh don't appear in the creator's
             top-countries list at all, so they contribute 0 overlap)

  average of [0.861, 0.049] (2 available components) = (0.861+0.049)/2 = **0.455**

### 4. creator_tier_fit — weight 0.15 — score 1.0
Creator's tier `macro` == brand's typical_creator_tier `macro` → same
position in the nano/micro/macro/mega order → 1.0 − 0/3 = **1.0**

### 5. semantic_similarity — weight 0.10 — score 0.7695
1 − cosine_distance from the Stage 3B pgvector search between the
creator's embedding and Zero Lifestyle's embedding = **0.76946...**

### 6. platform_match — weight 0.05 — score 1.0
Creator's primary_platform is `instagram`; brand's has_instagram = true →
**1.0**

### 7. geo_match — weight 0.05 — score 0.0
Brand's operating_area is `pakistan` (not "worldwide"). Creator's only
listed audience country is "United States", which doesn't match Pakistan
for any entry → best overlap = **0.0** (a real "no match found" zero, not
a skipped/unknown dimension)


## Step 2 — Weighted average (score_match(), pipeline/matching/scoring.py)

All 7 dimensions returned a real number this time (none were `None`), so
no reweighting is needed — every weight counts, and they already sum to
1.0:

```
total_score = 1.0×0.25 + 0.8525×0.20 + 0.455×0.20 + 1.0×0.15 + 0.7695×0.10 + 1.0×0.05 + 0.0×0.05
            = 0.2500  + 0.1705       + 0.0910      + 0.1500  + 0.07695      + 0.0500  + 0.0000
            = 0.78845
```


## Step 3 — Display (frontend/matches.html)

```js
function pct(score) { return Math.round((score || 0) * 100) + '%'; }
```

`Math.round(0.78845 × 100) = Math.round(78.845) = 79` → **"79% match"**


## Summary — what's driving this score up vs down

Pulling it up: exact niche match, exact tier match, exact platform match,
solid semantic similarity, and a fairly strong sponsorship_activity score
(0.85) — mostly the meta-ads component (active, no end date, recent, high
volume) plus recent Instagram partnership activity.

Pulling it down: geo_match is a hard 0.0 (creator's audience is 100% US,
brand operates in Pakistan), and audience_demographics is dragged to 0.455
by weak country overlap (only 4.9% of the brand's audience is US) — the
age-bucket component that would normally help/hurt this dimension is
silently skipped both here and inside sponsorship_activity's age-hop score,
because the brand's audience_age_groups still uses the old age taxonomy
(`teen`/`adult`/`young_adult`) instead of the new bucket labels
(`12_16`...`60_plus`) that `_AGE_BUCKET_RANGES` and the age-hop lookup
expect. This is a known, pre-existing data-staleness issue (flagged
earlier for a couple of specific brands), not a bug introduced by this
calculation.
