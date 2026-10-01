# Match Taglines

All "why it's a match" taglines generated in [pipeline/matching/match_text.py](pipeline/matching/match_text.py).

## How it works

- Every tagline whose condition is true is returned for the brand, sorted by **priority** (highest first). The backend has no limit.
- The **matches page** shows the top **5**. Clicking the brand name (or **"Show all N reasons"**) shows all of them.
- **Only-one groups**: some taglines check the same thing, so only one of them is ever shown. The first option (a, then b, then c) whose condition is true wins.

Placeholders: `{brand}` = brand name, `{similarity}` = `80-90%` or `90-100%`, `{window}` = `last month` / `last 3 months` / `last 6 months`.

---

## 1. Niche bridge

**Priority:** 10000 (always shown first)

**When:** the brand's niche is different from the creator's niche, but the brand has sponsored a creator (Instagram collaborator or RE creator) in the creator's niche.

> Although {brand} is a {brand niche} brand, it also sponsors creators in the {niche} niche, the same niche as you.

---

## 2. Recent paid partnership (only one of 3)

**When:** the brand has a paid-partnership Instagram post in the last 6 months. Uses the most recent post. "Similar" means creator content similarity ≥ 70%.

| Option | Condition | Tagline |
|---|---|---|
| a | 2+ similar creators on the latest post | {brand} ran a paid partnership with multiple creators whose content {similarity} matches yours within the {window}. |
| b | Exactly 1 similar creator | {brand} ran a paid partnership within the {window} with the creator whose content {similarity} matches yours. |
| c | No similar creator | {brand} ran a paid partnership within the {window}. |

**Priority** depends on how recent the latest post is:

| Window | a | b | c |
|---|---|---|---|
| Last month | 100 | 95 | 82 |
| Last 3 months | 92 | 90 | 75 |
| Last 6 months | 85 | 81 | 71 |

---

## 3. Creator size (only one of 3)

**When:** the creator's followers are within ±25% of the brand's average collaborator followers.

| Option | Priority | Condition | Tagline |
|---|---|---|---|
| a | 80 | A partner with similar content exists | {brand} has partnered with creators average ({N} followers), creators close to your size with content {similarity} similar to yours. |
| b | 80 | A same-niche partner exists (no similar-content one) | {brand} has partnered with creators average ({N} followers), creators close to your size with content type same as yours. |
| c | 50 | Size only | {brand} has partnered with creators average ({N} followers), creators close to your size. |

---

## 4. Verified contact

**Priority:** 75

**When:** the brand has a contact with a verified email, sponsorship contact confidence ≥ 70, who is still at the brand.

> MAVLIT has a verified contact for {brand}'s partnerships team.

---

## 5. Similar partners (only one of 3)

| Option | Priority | Condition | Tagline |
|---|---|---|---|
| a | 78 | 3+ partners with similar content | {N} creators with content {similarity} similar to yours have partnered with {brand}. |
| b | 70 | 5+ same-niche partners on posts with sponsorship confidence ≥ 90 | {brand} has partnered with a creator in the {niche} niche, the same as yours. |
| c | 65 | 1–2 partners with similar content | {brand} has partnered with a creator whose content {similarity} matches yours. |

---

## 6. Follower range

**Priority:** 69

**When:** the creator's followers fall inside the brand's collaborator follower range (YouTube range for YouTube creators, Instagram range for everyone else).

> {brand} works with creators that has followers range same as yours.

---

## 7. YouTube sponsor

**Priority:** 45

**When:** the brand has a YouTube sponsorship with confidence ≥ 0.7.

> {brand} also sponsors YouTube creators.

---

## 8. Brand tier (only one of 2)

| Option | Priority | Condition | Tagline |
|---|---|---|---|
| a | 40 | `brand_tier` = lower-range | {brand} is a smaller brand, so creators can typically reach decision-makers directly. |
| b | 35 | `brand_tier` = midlower-range | {brand} is a growing brand where creator outreach is still realistic. |

---

## 9. Tag overlap

**Priority:** 33 (only the first matching tag is shown)

**When:** a brand tag and one of the creator's sub-niches share words.

> {brand} focuses on {brand tag}, which overlaps with your content ({creator tag}).

---

## 10. Same niche

**Priority:** 30

**When:** the brand's niche is exactly one of the creator's niches.

> {brand} is a {niche} brand, the same niche as you.

---

## 11. Latest product

**Priority:** 29

**When:** `brands_raw.latest_product` is not null.

> {brand}'s latest product is {latest_product}, a timely hook for your pitch.

---

## 12. Target audience gender (only one of 2)

**Priority:** 69.3 (just above 6. Follower range)

**When:** `brands_raw.target_audience_gender` is `male`, `female` or `both`.

| Option | Condition | Tagline |
|---|---|---|
| a | The brand's gender matches the user's gender (or is `both`), **or** the user has no male/female gender, **or** no creator of the user's gender was found | {brand}'s target audience is {men / women / both men and women}. |
| b | The brand's gender does **not** match the user's, but at least one brand creator (Instagram collaborator or RE creator, sponsorship confidence ≥ 90) has the user's gender | {brand}'s target audience is {women / men}, but it has already backed {a male creator / N male creators} in {niche}, so the door is open for creators like you. |

If **both** the brand's gender and a creator's gender match the user, only **a** is shown.

---

## 13. Target audience age (only one of 3)

**Priority:** 69.2 (right after 12, above 6. Follower range)

| Option | Condition | Tagline |
|---|---|---|
| a | Min and max age set | {brand} targets an audience aged {min}-{max}. |
| b | Only min age set | {brand} targets an audience aged {min}+. |
| c | Only max age set | {brand} targets an audience aged up to {max}. |

---

## 14. Product audience gender

**Priority:** 69.1 (right after 13, above 6. Follower range)

**When:** `brands_raw.product_audience_gender` is `male`, `female` or `both`.

> {brand}'s products are made for {men / women / both men and women}.

---

## Priority order (all taglines)

| Priority | Tagline |
|---|---|
| 10000 | 1. Niche bridge |
| 100 | 2a. Recent partnership, multiple similar creators (last month) |
| 95 | 2b. Recent partnership, one similar creator (last month) |
| 92 | 2a. Recent partnership, multiple similar creators (last 3 months) |
| 90 | 2b. Recent partnership, one similar creator (last 3 months) |
| 85 | 2a. Recent partnership, multiple similar creators (last 6 months) |
| 82 | 2c. Recent partnership (last month) |
| 81 | 2b. Recent partnership, one similar creator (last 6 months) |
| 80 | 3a / 3b. Creator size with similar content / same niche |
| 78 | 5a. 3+ similar creators |
| 75 | 2c. Recent partnership (last 3 months) |
| 75 | 4. Verified contact |
| 71 | 2c. Recent partnership (last 6 months) |
| 70 | 5b. 5+ same-niche partners |
| 69.3 | 12. Target audience gender |
| 69.2 | 13. Target audience age |
| 69.1 | 14. Product audience gender |
| 69 | 6. Follower range |
| 65 | 5c. 1–2 similar creators |
| 50 | 3c. Creator size only |
| 45 | 7. YouTube sponsor |
| 40 | 8a. Smaller brand |
| 35 | 8b. Growing brand |
| 33 | 9. Tag overlap |
| 30 | 10. Same niche |
| 29 | 11. Latest product |
