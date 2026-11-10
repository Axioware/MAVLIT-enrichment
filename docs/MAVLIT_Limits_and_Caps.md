# MAVLIT: Limits and Caps

Every limit the platform applies, with its default value. Updated after the removal of the contact-reveal limit.

Each value can be changed by an environment variable on the server without changing code. An invalid value stops the app at start, instead of failing later.

## 1. Per-user daily limits

Counted before the paid call and reset at midnight UTC. Over the limit the user gets a "daily limit" message (HTTP 429). A value of 0 turns the feature off.

| Limit | Default | Setting |
|---|---|---|
| Pitches | 30 a day | `PITCHES_PER_DAY` |
| Rate estimates | 20 a day | `RATE_INTELLIGENCE_PER_DAY` |
| Profile generations | 20 a day | `DESCRIPTION_GENERATIONS_PER_DAY` |
| Contract reviews | 10 a day | `CONTRACT_ADVICE_PER_DAY` |
| Match refreshes | 10 a day | `MATCH_REFRESH_PER_DAY` |
| Own-brand lookups | 10 a day | `OWN_BRANDS_PER_DAY` |
| Brand contacts opened | No limit | (removed) |

## 2. Brand contacts: no limit

There is no limit on how many brands' contacts a user can open. Opening a brand shows all of its contacts: the main contact and the "View more" list.

- **Who can see contacts:** only for brands the user saved, brands in their matches, and brands they added themselves. For any other brand the contacts are left out, with no error.
- **Usage record:** each brand opened is noted once per day for the admin and usage report. It never refuses anything.

## 3. Per-user caps

| Cap | Value |
|---|---|
| Pitches running at once | 1 per user (a second gets an "already running" message). A lock older than about 5 minutes is taken over. |
| Saved brands | 500 |
| Saved Instagram posts | 10 minimum (nothing can be deleted at or below this), 50 maximum |

## 4. Security limits

| Limit | Value |
|---|---|
| Failed sign-ins | 5 per email and 30 per IP address in 15 minutes, then a lockout |
| Admin and operator logins | 5 failures in 15 minutes |
| Forgot-password emails | 3 per email and 10 requests per IP address per hour |
| Early-access form | 10 sign-ups per IP address per hour, one per email |
| Sign out everywhere | 10 per hour |
| Password length | New-user and reset pages: at least 6. Changing a password in the app: at least 12. Maximum 72 bytes. |
| Invitation link | Single use, valid 30 days by default |
| Password-reset link | Single use, valid 60 minutes |

## 5. Server capacity

| Limit | Value |
|---|---|
| Slow routes at once (profile generation, refresh, pitches, rate estimates) | 12. The 13th gets "server busy" (503) and is asked to retry in 5 seconds. |
| Pitch streams | 6 at once, each stopped after 150 seconds |
| User background jobs | 4 workers, up to 50 waiting. A full queue returns 503 and the daily limit is not charged. |
| Operator jobs | 1 worker, up to 10 waiting |
| Job lock considered dead after | 30 minutes |
| Request size | 16 KB default; 32 KB profile and pitches; 64 KB contract advice and pitch edits; 256 KB bulk creator upload. Larger gets 413. |
| Paging | Maximum offset 10,000 |
| Database (web) | Pool of 10 plus 5 connections; 15 seconds per query; 5 seconds lock wait |
| Database (background jobs) | 30 minutes per statement; 5 minutes idle |

## 6. Data-processing limits

| Area | Limit |
|---|---|
| Matching | The 500 nearest brands are scored. Up to 300 go to the AI, 20 per call. Brands scoring under 55 are hidden. |
| Profile generation | 20 posts read |
| Seed creators | 40 posts on the first read. A refresh reads at most 60 new posts, every 30 days. |
| Brand posts | 40 read (60 for beach brands) |
| Contacts per brand | Apollo search up to 50 candidates (free). At most 5 are paid-enriched, verified and checked on LinkedIn. |
| Website check | At most 5 pages per brand |

## 7. Caches

| Cache | Limit |
|---|---|
| Public niche lists | Kept 5 minutes, up to 200 entries |
| Post thumbnails | Up to 300 pictures or 64 MB, 2 MB per picture, kept 6 hours |
