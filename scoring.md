# Brand Initial Scoring Formula (0 – 100 points)

---

## Purpose

Score each brand to answer one question:

> **How likely is this brand to actively pay content creators for sponsorships right now?**

A high score means:
- The brand has a proven track record of paying content creators (YouTube, Instagram)
- The brand is currently running paid advertising (Meta Ads with live spend)
- The brand is large enough and established enough to have a real budget
- We have enough enriched data to trust the score

> **V1 scope:** TikTok and Twitter are not scored in V1 — those signals aren't collected reliably enough yet. All of their point weight has been redistributed into YouTube, Instagram, and the remaining social-presence signals below. `tiktok_checked`/`twitter_checked` are also not required before scoring a brand.

This score is **not** about how well-known a brand is. A smaller Shopify brand actively running 5 YouTube sponsorships this month scores higher than a famous brand with zero influencer activity.

Brands that reach HOT / WARM are sent to Apollo for contact enrichment. The goal is to connect **content creators (this platform's users) with brands that are actively buying influencer marketing right now.**

---

## When to Score

Only score a brand after its key enrichment steps are complete. A brand with `youtube_checked = false` and `instagram_checked = false` may score 0 not because it has no influencer activity but because we haven't fetched the data yet.

Minimum enrichment required before scoring:
- `youtube_checked = true`
- `instagram_checked = true`
- `meta_ads_fetched = true`

Track how many of these are complete in the `enrichment_completeness` output column (0–3). Prefer scoring brands where all 3 are true.

---

## Score Breakdown

| Section | Max Points | What it signals |
|---------|-----------|-----------------|
| 1. Influencer Buying Activity | 50 | Brand has actively paid content creators |
| 2. Advertising Budget (Meta) | 15 | Brand has current money to spend |
| 3. Brand Scale & Legitimacy | 25 | Brand is real, established, and worth pursuing |
| 4. Contact Reachability | 10 | Apollo can find a decision-maker |
| **Total** | **100** | |

> Section 2 (Meta Ads) is intentionally capped at 15 pts — below Instagram's 25 pts. Ad spend proves budget but not influencer intent. A brand with only Meta Ads is a cold-call. A brand with paid YouTube/Instagram sponsorships is a warm lead.

---

## Score Bands

| Score | Band | Meaning | Action |
|-------|------|---------|--------|
| 70 – 100 | HOT | Actively buying influencer marketing right now | Immediate Apollo enrichment + personalised outreach |
| 50 – 69 | WARM | Strong influencer history, solid ad budget | Standard Apollo enrichment + outreach sequence |
| 30 – 49 | COOL | Some signals, limited recency or scale | Low priority, revisit after more enrichment |
| 0 – 29 | COLD | No meaningful influencer activity found | Skip — no evidence of creator spending |

---

## 1. Influencer Buying Activity — 50 pts max

This section answers: **has this brand paid content creators before, and how recently?**

Recency is weighted heavily throughout. A brand sponsoring creators this month is far more valuable than one that ran a campaign 3 years ago.

---

### YouTube Sponsorships — 25 pts max
Source: `youtube_sponsorships` table

#### 1a. Recency — most recent `published_at` (0 – 10 pts)

Most important sub-signal. A brand with 1 sponsorship last month outscores a brand with 10 from 5 years ago.

| Days since most recent sponsorship | Points |
|------------------------------------|--------|
| ≤ 30 days | 10 |
| 31 – 90 days | 8 |
| 91 – 180 days | 5 |
| 181 – 365 days | 3 |
| 1+ year or no data | 0 |

#### 1b. Sponsorship Count (0 – 8 pts)

| Count | Points |
|-------|--------|
| 0 | 0 |
| 1 – 2 | 4 |
| 3 – 9 | 6 |
| 10+ | 8 |

#### 1c. Creator Audience Reach (0 – 7 pts)

Brands sponsoring large channels have bigger influencer budgets and are more serious buyers.

| Best subscriber count across all sponsoring channels | Points |
|------------------------------------------------------|--------|
| No data | 0 |
| Any channel 10k – 99k | 3 |
| Any channel 100k – 999k | 5 |
| Any channel 1M+ | 7 |

---

### Instagram Paid Partnerships — 25 pts max
Source: `instagram_posts` + `brand_instagram_users` tables

#### 2a. Paid Partnership Posts (0 – 12 pts)

`paid_partnership = true` is officially labelled by Meta — the strongest Instagram signal that the brand paid a creator.

| Count of posts with `paid_partnership = true` | Points |
|-----------------------------------------------|--------|
| 0 | 0 |
| 1 – 2 | 6 |
| 3 – 5 | 9 |
| 6+ | 12 |

#### 2b. Sponsors Field Populated (0 – 3 pts)

Brand tagged as a sponsor in creator posts — additional confirmation of paid activity.

| Condition | Points |
|-----------|--------|
| Any `instagram_post` has `sponsors` field non-empty | +3 |

#### 2c. Creator Network Size in `brand_instagram_users` (0 – 7 pts)

Brands with larger creator networks run systematic influencer programs, not one-off posts.

| Count of linked creators | Points |
|--------------------------|--------|
| 0 | 0 |
| 1 – 9 | 4 |
| 10+ | 7 |

#### 2d. Collaboration Signals (0 – 3 pts)

| Condition | Points |
|-----------|--------|
| Any post has `tagged_users` or `coauthor_producers` non-empty | +3 |

---

## 2. Advertising Budget — 15 pts max

This section answers: **does this brand have active money to spend right now?**

It is a supporting signal, not a primary one. A brand with heavy Meta spend but zero influencer activity has budget but has not yet chosen the influencer channel.

Source: `meta_ads` table

### Ad Volume (0 – 5 pts)

| Total ad count | Points |
|----------------|--------|
| 0 | 0 |
| 1 – 4 | 2 |
| 5 – 9 | 3 |
| 10+ | 5 |

### Active Ads — `end_date IS NULL` (0 – 3 pts)

Ads with no end date are currently running — strongest signal of live budget.

| Count of ads with `end_date IS NULL` | Points |
|--------------------------------------|--------|
| 0 | 0 |
| 1 – 5 | 2 |
| 6+ | 3 |

### Recency — most recent `start_date` (0 – 3 pts)

| Days since most recent ad start date | Points |
|--------------------------------------|--------|
| ≤ 30 days | 3 |
| 31 – 90 days | 2 |
| 91 – 180 days | 1 |
| 180+ days or no ads | 0 |

### Spend — sum of `spend.lower_bound` across all ads (0 – 2 pts)

Meta returns spend as `{"lower_bound": "0", "upper_bound": "99"}`. Sum `lower_bound` across all ads (conservative estimate of total spend).

| Sum of `spend.lower_bound` | Points |
|----------------------------|--------|
| $0 | 0 |
| $1 – $999 | 1 |
| $1,000+ | 2 |

### Impressions — sum of `impressions.lower_bound` across all ads (0 – 1 pt)

Meta returns impressions as `{"lower_bound": "1000", "upper_bound": "1999"}`.

| Sum of `impressions.lower_bound` | Points |
|----------------------------------|--------|
| 0 – 99,999 | 0 |
| 100,000+ | 1 |

### Platform Coverage (0 – 1 pt)

`publisher_platforms` can contain: `facebook`, `instagram`, `audience_network`, `messenger`, `threads`.

| Condition | Points |
|-----------|--------|
| Ads running on both `facebook` AND `instagram` | +1 |

---

## 3. Brand Scale & Legitimacy — 25 pts max

This section answers: **is this brand real, established, and large enough to have an influencer budget?**

---

### Tranco Rank — 10 pts max
Source: `brands_raw.tranco_rank`

Best available proxy for website traffic and brand size when revenue data is absent. A brand in the top 50k is a real, established business with a real audience to reach.

| Rank | Points |
|------|--------|
| Top 10,000 | 10 |
| 10,001 – 50,000 | 7 |
| 50,001 – 100,000 | 5 |
| 100,001 – 500,000 | 3 |
| 500,001+ | 1 |
| Not in Tranco list | 0 |

---

### E-commerce Platform — 8 pts max
Source: `brands_raw.is_shopify` / `brands_raw.is_woocommerce`

Not additive — take the higher one.

| Condition | Points |
|-----------|--------|
| `is_shopify = true` | 8 |
| `is_woocommerce = true` | 5 |
| Neither | 0 |

Shopify brands are direct-to-consumer with measurable influencer ROI (tracked via affiliate links, promo codes). They are the most motivated buyers of influencer marketing and the easiest to reach through Apollo.

---

### Social Media Presence — 7 pts max
Source: `brands_raw` social handle columns

`linkedin_id` is excluded here — it is scored separately in Section 4 (Reachability). TikTok/Twitter handles are not scored in V1. A brand active across multiple social platforms is more likely to value creator content across those same platforms.

| Handle present | Points |
|----------------|--------|
| `instagram_handle` | +3 |
| `youtube_channel_id` | +2 |
| `facebook_page` | +2 |

---

## 4. Contact Reachability — 10 pts max

This section answers: **can Apollo actually find a decision-maker at this brand?**

Source: `brands_raw`

| Condition | Points |
|-----------|--------|
| `linkedin_id` present | +5 |
| `facebook_page_id` resolved (numeric ID known) | +3 |
| `is_shopify = true` | +2 (Shopify stores usually have findable team pages) |

---

## Key Rules

**Rule 1 — No influencer activity = COLD regardless of total score.**
A brand scoring 0 in Section 1 is deprioritized regardless of Meta Ads spend or Tranco rank. No YouTube or Instagram creator spending = no proven intent to buy influencer marketing. They may have budget but they are not yet in the market.

**Rule 2 — Recency beats volume.**
An old influencer campaign is weak signal. A brand that sponsored 1 YouTuber last month is a better target than a brand that sponsored 20 two years ago. Recency scoring reflects this in both YouTube and Meta Ads sections.

**Rule 3 — Meta Ads prove budget, not intent.**
Section 2 is capped at 15 pts, always below Section 1. A brand spending heavily on Meta Ads but running no creator campaigns is a cold-call. One with both is a warm lead.

**Rule 4 — Shopify = priority target.**
Shopify brands are direct-to-consumer, have clear influencer ROI tracking, and are easier to reach. Flag these even at COOL band if they have any influencer signals.

**Rule 5 — Score reliability depends on data completeness.**
A brand with `youtube_checked = false` cannot be scored accurately on Section 1. Use `enrichment_completeness` to filter before sending to Apollo.

---

## Output Table: `initial_brand_score`

```
Column                  Type        Description
----------------------  ----------  -----------------------------------------------
id                      SERIAL      Primary key
brand_raw_id            INTEGER     FK → brands_raw.id  (UNIQUE — one row per brand)

influencer_score        INTEGER     0 – 50   (Section 1)
ad_spend_score          INTEGER     0 – 15   (Section 2)
legitimacy_score        INTEGER     0 – 25   (Section 3)
reachability_score      INTEGER     0 – 10   (Section 4)

total_score             INTEGER     0 – 100
score_band              TEXT        'HOT' | 'WARM' | 'COOL' | 'COLD'

enrichment_completeness INTEGER     0 – 3  (count of: youtube_checked, instagram_checked,
                                            meta_ads_fetched = true)

score_details           JSONB       Full sub-component breakdown for auditing
scored_at               TIMESTAMPTZ When this score was last computed
```

`score_details` example:
```json
{
  "youtube": {
    "recency_days": 22,
    "recency_pts": 10,
    "sponsorship_count": 4,
    "count_pts": 6,
    "max_subscriber_count": 1400000,
    "subscriber_pts": 7,
    "total": 23
  },
  "instagram": {
    "paid_partnership_posts": 4,
    "paid_pts": 9,
    "sponsors_populated": true,
    "sponsors_pts": 3,
    "creator_network_count": 14,
    "creator_pts": 7,
    "collab_signals": true,
    "collab_pts": 3,
    "total": 22
  },
  "meta_ads": {
    "ad_count": 11,
    "volume_pts": 5,
    "active_no_end_date": 7,
    "active_pts": 3,
    "recency_days": 18,
    "recency_pts": 3,
    "spend_sum_lower": 1240,
    "spend_pts": 2,
    "impressions_sum_lower": 145000,
    "impressions_pts": 1,
    "both_platforms": true,
    "platform_pts": 1,
    "total": 15
  },
  "legitimacy": {
    "tranco_rank": 38000,
    "tranco_pts": 7,
    "is_shopify": true,
    "ecommerce_pts": 8,
    "social_handles": ["instagram_handle", "youtube_channel_id", "facebook_page"],
    "social_pts": 7,
    "total": 22
  },
  "reachability": {
    "has_linkedin": true,
    "linkedin_pts": 5,
    "has_facebook_page_id": true,
    "fb_pts": 3,
    "is_shopify": true,
    "shopify_pts": 2,
    "total": 10
  }
}
```

---

## Scoring Pipeline

```
All enrichment steps complete (youtube + instagram + meta_ads)
                        ↓
              Run scoring formula
                        ↓
         UPSERT into initial_brand_score
                        ↓
     Filter: total_score >= 50 AND influencer_score > 0
                        ↓
            Send to Apollo for contact enrichment
                        ↓
         Outreach to content creator partnerships
```

---

## Rescoring Policy

- Score row is **replaced** (UPSERT on `brand_raw_id`) every time scoring runs.
- Re-run after any enrichment step flips to true.
- Only send to Apollo if `total_score >= 50` AND `influencer_score > 0` AND `enrichment_completeness >= 2`.

---

## Data Sources Reference

| Table | Key fields used |
|-------|----------------|
| `brands_raw` | `tranco_rank`, `is_shopify`, `is_woocommerce`, `instagram_handle`, `youtube_channel_id`, `facebook_page`, `facebook_page_id`, `linkedin_id` |
| `youtube_sponsorships` | `brand_raw_id`, `subscriber_count`, `published_at` |
| `instagram_posts` | `brand_raw_id`, `paid_partnership`, `sponsors`, `tagged_users`, `coauthor_producers` |
| `brand_instagram_users` | `brand_raw_id` — count of linked creators |
| `meta_ads` | `brand_raw_id`, `end_date`, `start_date`, `publisher_platforms`, `spend`, `impressions` |

*(`tiktok_posts` and `twitter_posts` are not used by scoring in V1.)*
