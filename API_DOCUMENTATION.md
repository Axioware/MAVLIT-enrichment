# MAVLIT API Documentation

Developer reference for the FastAPI backend, the bundled HTML frontend, and the enrichment services used by MAVLIT.

## Contents

1. [Runtime and conventions](#runtime-and-conventions)
2. [Authentication](#authentication)
3. [Frontend API by feature](#frontend-api-by-feature)
4. [Operations and enrichment API](#operations-and-enrichment-api)
5. [Frontend page routes](#frontend-page-routes)
6. [External services](#external-services)
7. [Frontend integration flows](#frontend-integration-flows)
8. [Gaps and maintenance flags](#gaps-and-maintenance-flags)

## Runtime and conventions

- Application entry point: `api.app:app`.
- Local frontend pages are served by the same FastAPI process. The API uses relative paths in the bundled HTML files.
- JSON errors use FastAPI's default shape: `{ "detail": "human-readable message" }`.
- Authenticated browser requests must use `credentials: "include"` so the `access_token` cookie is sent.
- In production, configure `FRONTEND_ORIGINS` for separate frontend origins. Production cookies use `SameSite=None; Secure`; development cookies use `SameSite=Lax`.
- Background jobs are stored in an in-memory `_jobs` dictionary. They are lost on restart and are not shared between worker processes.
- Unless a response model is called out, response objects are returned as plain JSON dictionaries.

## Authentication

Authentication is email/password plus a seven-day, HttpOnly JWT cookie. The frontend never reads the JWT.

### `POST /auth/login`

- **Purpose:** Validate credentials, set the `access_token` cookie, and return the complete creator profile.
- **Auth:** Public.
- **Request body:** `{ "email": "creator@example.com", "password": "..." }` (`LoginRequest`). Email is trimmed and lowercased.
- **Response:** `200`, `CreatorProfileResponse`.
- **Example request:**

  ```json
  { "email": "creator@example.com", "password": "correct horse battery staple" }
  ```

- **Example response:**

  ```json
  {
    "id": 12,
    "email": "creator@example.com",
    "is_active": true,
    "full_name": "Avery Stone",
    "creator_handle": "averystone",
    "creator_tier": "micro",
    "created_at": "2026-09-24 10:00:00+00:00",
    "updated_at": null
  }
  ```

- **Errors:** `401` invalid credentials; `403` account deactivated; `422` malformed body.
- **Frontend:** `frontend/login.html`.

### `GET /auth/me`

- **Purpose:** Check the current session and return a small identity object.
- **Auth:** Valid `access_token` cookie required.
- **Request:** No body or parameters.
- **Response:** `200` `{ "id": 12, "email": "creator@example.com", "name": "Avery Stone" }`.
- **Errors:** `401` missing, invalid, expired, inactive, or unknown user.
- **Frontend:** All authenticated-aware pages: `login.html`, `index.html`, `add-creators.html`, `creator-profile.html`.

### `POST /auth/logout`

- **Purpose:** Clear the session cookie.
- **Auth:** No dependency is required; it is safe to call when already signed out.
- **Request:** No body.
- **Response:** `200` `{ "message": "Logged out" }`.
- **Errors:** Normally none beyond server errors.
- **Frontend:** `index.html`, `add-creators.html`; login/profile pages redirect to sign-in when needed.

## Frontend API by feature

### Creator onboarding and profile

#### `GET /creator-profile/me`

- **Purpose:** Load the logged-in creator's full profile for editing.
- **Auth:** Required.
- **Request:** No body or parameters.
- **Response:** `200`, `CreatorProfileResponse`.
- **Example response:**

  ```json
  {
    "id": 12,
    "email": "creator@example.com",
    "is_active": true,
    "full_name": "Avery Stone",
    "creator_handle": "averystone",
    "content_niche": "fitness",
    "primary_platform": "instagram,youtube",
    "follower_count": 42000,
    "instagram_handle": "averystone",
    "instagram_followers": 42000,
    "instagram_sub_niches": ["strength training"],
    "youtube_channel_name": null,
    "creator_tier": "micro",
    "content_tags": null,
    "created_at": "2026-09-24 10:00:00+00:00",
    "updated_at": "2026-09-24 10:15:00+00:00"
  }
  ```

- **Errors:** `401` unauthenticated; `422` only if response data cannot satisfy the schema.
- **Frontend:** `frontend/creator-profile.html`.

#### `PUT /creator-profile/me`

- **Purpose:** Create or update the current creator's profile. The creator tier is calculated synchronously; creator signals, tags, and embedding refresh in a background task.
- **Auth:** Required.
- **Request body:** `CreatorProfileRequest`. Required fields are `full_name` and `creator_handle`; all other fields are optional.

  ```json
  {
    "full_name": "Avery Stone",
    "creator_handle": "averystone",
    "age": 29,
    "gender": "female",
    "content_niche": "fitness",
    "content_description": "Strength and mobility for busy professionals",
    "excluded_categories": ["gambling"],
    "instagram_handle": "averystone",
    "instagram_followers": 42000,
    "primary_platform": "instagram",
    "follower_count": 42000,
    "instagram_primary_niche": "fitness",
    "instagram_sub_niches": ["strength training"],
    "instagram_excluded_categories": ["gambling"],
    "instagram_description": "Practical strength training at home",
    "instagram_description_mode": "manual",
    "instagram_audience_gender_male_pct": 0.35,
    "instagram_audience_gender_female_pct": 0.65,
    "youtube_channel_name": null,
    "youtube_subscribers": null,
    "youtube_primary_niche": null,
    "youtube_sub_niches": null,
    "youtube_excluded_categories": null,
    "youtube_description": null,
    "youtube_description_mode": null,
    "youtube_audience_gender_male_pct": null,
    "youtube_audience_gender_female_pct": null
  }
  ```

- **Response:** `200`, updated `CreatorProfileResponse`.
- **Errors:** `401` unauthenticated; `422` invalid types or missing required fields.
- **Frontend:** `frontend/creator-profile.html`.

#### `POST /creator-profile/me/generate-description`

- **Purpose:** Generate a platform description from public creator content before saving the profile.
- **Auth:** Required.
- **Request body:** `{ "platform": "instagram", "username": "averystone", "niche": "fitness" }`.
- **Response:** `200`, `{ "platform": "instagram", "description": "...", "posts_analyzed": 12, "videos_analyzed": null, "bio_found": true }`.
- **Errors:** `400` platform is not `instagram` or `youtube`, or the generator rejects the input; `401` unauthenticated; `502` upstream generation failure; `422` malformed body.
- **Frontend:** `frontend/creator-profile.html`.

#### `GET /creator-sub-niches`

- **Purpose:** Return distinct brand tag values used by the profile sub-niche picker.
- **Auth:** Public.
- **Response:** `200` `{ "niches": ["strength training", "streetwear"] }`.
- **Frontend:** `frontend/creator-profile.html`.

#### `GET /brand-niches`

- **Purpose:** Return distinct primary niche values supplied through `content_creator_re.niche`. These values feed the creator profile picker and matching hard filter.
- **Auth:** Public.
- **Request:** No body or parameters.
- **Response:** `200` `{ "niches": ["beauty", "fitness", "music"] }`.
- **Errors:** Database failures may produce `500`.
- **Frontend status:** The profile page currently has a hardcoded primary-niche list; this endpoint is the database-backed source for replacing it.

#### `GET /brand-niche-tags`

- **Purpose:** Return all brand tags and a tag list grouped by primary niche.
- **Auth:** Public.
- **Response:** `200` `{ "tags": ["streetwear"], "tags_by_niche": { "fashion": ["streetwear"] } }`.
- **Frontend status:** Not currently called; `creator-profile.html` uses `/creator-sub-niches` instead.

### Brand catalog and matching

#### `GET /api/brand-catalog`

- **Purpose:** Public, paginated browsing of `brands_raw` records.
- **Auth:** Public.
- **Query parameters:** `q` optional case-insensitive name substring; `niche` optional exact match; `limit` default `50`, range `1..200`; `offset` default `0`, minimum `0`.
- **Example request:** `GET /api/brand-catalog?q=studio&niche=fitness&limit=20&offset=0`.
- **Response:** `200` `{ "total": 1, "limit": 20, "offset": 0, "brands": [{ "id": 7, "name": "Studio Fit", "niche": "fitness", "website": "https://studio.example", "domain": "studio.example", "country": "US" }] }`.
- **Errors:** `422` invalid query bounds.
- **Frontend:** `frontend/brand-catalog.html`.

#### `GET /api/brand-catalog/niches`

- **Purpose:** Populate the catalog niche filter.
- **Auth:** Public.
- **Response:** `200` `{ "niches": ["beauty", "fitness"] }`.
- **Frontend:** `frontend/brand-catalog.html`.

#### `GET /api/brand-catalog/{brand_id}`

- **Purpose:** Show raw catalog details for one brand.
- **Auth:** Public.
- **Response:** `200` includes `id`, `name`, `niche`, `description`, `website`, `domain`, `country`, `headquarters`, social identifiers, `brand_tier`, `geo_reach_score`, `geo_reach_label`, `meta_ads_total`, and `meta_ads`.
- **Example response:**

  ```json
  {
    "id": 7,
    "name": "Studio Fit",
    "niche": "fitness",
    "description": "Training equipment for home gyms",
    "website": "https://studio.example",
    "domain": "studio.example",
    "country": "US",
    "headquarters": null,
    "instagram_handle": "studiofit",
    "youtube_channel_id": null,
    "facebook_page": null,
    "brand_tier": "mid",
    "geo_reach_score": 72.0,
    "geo_reach_label": "strong",
    "meta_ads_total": 3,
    "meta_ads": []
  }
  ```

- **Errors:** `404` brand not found; `422` invalid integer path.
- **Frontend:** `frontend/brand-catalog.html`.
- **Important:** `meta_ads` is currently a fixed three-example placeholder set shared by every brand; it is not queried from `meta_ads` for this endpoint.

#### `GET /matches/me`

- **Purpose:** Compute ranked Stage 3 brand matches for the current creator.
- **Auth:** Required.
- **Query parameters:** `limit` default `20`; `offset` default `0`.
- **Example request:** `GET /matches/me?limit=20&offset=0`.
- **Response:** `200`, `MatchesResponse`.

  ```json
  {
    "matches": [
      {
        "brand_raw_id": 7,
        "brand_name": "Studio Fit",
        "niche": "fitness",
        "total_score": 86.4,
        "dimensions": {
          "niche": { "score": 100, "weight": 0.4 },
          "audience": { "score": 78, "weight": 0.3 }
        },
        "reasons": ["Niche overlap", "Audience gender fit"]
      }
    ],
    "cached": false,
    "computed_at": "2026-09-24T10:20:00+00:00"
  }
  ```

- **Errors:** `401` unauthenticated; `422` invalid query values.
- **Frontend:** `frontend/matches.html`.
- **Behavior:** Always computes live today; `cached` is always `false`.

#### `POST /matches/me/refresh`

- **Purpose:** Recompute the current creator's matches.
- **Auth:** Required.
- **Query parameters:** `limit` default `20`.
- **Response/errors:** Same `MatchesResponse` and `401`/`422` behavior as `GET /matches/me`.
- **Frontend:** `frontend/matches.html`.
- **Maintenance flag:** Currently functionally identical to `GET /matches/me`; retained for a future cache invalidation contract.

#### `GET /brands/{brand_id}`

- **Purpose:** Return matching/advisory-oriented brand detail, including creator tier fit, sponsorship activity, platform signals, and a verified top contact.
- **Auth:** Required.
- **Response:** `200`, `BrandDetailResponse`: `brand_id`, `name`, `short_bio`, `description`, `niche`, `creator_tier_fit`, `last_partnership_date`, `meta_ads_active`, `platform_signals`, `sponsorship_activity_score`, and optional `top_contact`.
- **Example response:**

  ```json
  {
    "brand_id": 7,
    "name": "Studio Fit",
    "short_bio": "Home fitness equipment",
    "description": "...",
    "niche": "fitness",
    "creator_tier_fit": { "typical_tier": "micro", "followers_low": 10000, "followers_high": 100000 },
    "last_partnership_date": "2026-05-10",
    "meta_ads_active": true,
    "platform_signals": { "instagram": "strong", "youtube": "moderate" },
    "sponsorship_activity_score": 72.5,
    "top_contact": { "name": "Jordan Lee", "email": "jordan@studio.example", "title": "Head of Marketing" }
  }
  ```

- **Errors:** `401` unauthenticated; `404` brand not found.
- **Frontend status:** No current bundled page calls this route; intended for a creator brand-detail workflow.

### Saved brands, pitches, dashboard, and advisory

The following endpoints are authenticated and implemented for API consumers, but are not currently called by the bundled HTML pages.

#### `POST /saved-brands`

- **Purpose:** Save a brand for the current creator; idempotent.
- **Auth:** Required.
- **Body:** `{ "brand_id": 7 }` (`SaveBrandRequest`).
- **Response:** `201` `{ "brand_id": 7, "saved": true }`.
- **Errors:** `401` unauthenticated; `404` brand not found; `422` malformed body.

#### `GET /saved-brands/me`

- **Purpose:** List saved brands newest first.
- **Auth:** Required.
- **Response:** `200`, array of `{ "brand_id", "name", "niche", "short_bio", "saved_at" }`.
- **Errors:** `401` unauthenticated.

#### `DELETE /saved-brands/{brand_id}`

- **Purpose:** Remove a saved brand.
- **Auth:** Required.
- **Response:** `200` `{ "brand_id": 7, "saved": false }`.
- **Errors:** `401` unauthenticated; `404` brand is not saved.

#### `POST /pitches`

- **Purpose:** Generate and persist a personalized pitch. The saved status is currently `proposal_sent`.
- **Auth:** Required.
- **Body:** `PitchRequest`: `is_custom`, `story`, optional `product_reference`, `past_brand_partnership`, `content_link`; non-custom requests require `brand_id`, while custom requests require `custom_brand_name` and omit `brand_id`.
- **Example request:**

  ```json
  {
    "is_custom": false,
    "brand_id": 7,
    "story": "I use Studio Fit equipment in my mobility series.",
    "product_reference": "Adjustable dumbbells",
    "past_brand_partnership": null,
    "content_link": "https://instagram.com/p/example"
  }
  ```

- **Response:** `200`, `PitchResponse` with `id`, brand fields, submitted story fields, optional contact fields, generated `pitch_text`, `status`, and `created_at`.
- **Errors:** `400` invalid custom/non-custom combination; `401` unauthenticated; `404` brand not found; `502` LLM generation failed and no pitch is saved; `422` malformed body.

#### `GET /pitches/me`

- **Purpose:** Return the current creator's pitch history newest first.
- **Auth:** Required.
- **Response:** `200`, array of `PitchResponse`.
- **Errors:** `401` unauthenticated.

#### `GET /dashboard/me`

- **Purpose:** Return summary counts for the creator dashboard.
- **Auth:** Required.
- **Response:** `200` `{ "brand_matches": 37, "active_deals": 3, "saved_brands": 8, "verified_contacts": 5 }`.
- **Errors:** `401` unauthenticated.

#### `POST /rate-intelligence`

- **Purpose:** Estimate a creator rate for a brand, platform, and deliverable; saves the estimate.
- **Auth:** Required.
- **Body:** `{ "brand_id": 7, "platform": "instagram", "deliverable_type": "1 feed post + 3 stories", "exclusivity": null, "usage": "organic only", "duration_months": 1 }`.
- **Response:** `200`, `{ "id": 7, "rate_min": 800, "rate_max": 1500, "currency": "USD", "reasoning": "...", "created_at": "..." }`.
- **Errors:** `401` unauthenticated; `404` brand not found; `502` LLM failure; `422` malformed body.

#### `POST /contract-advice`

- **Purpose:** Review pasted contract text and save a plain-language review. This is not legal advice.
- **Auth:** Required.
- **Body:** `{ "contract_text": "full contract text..." }`.
- **Response:** `200`, `{ "id": 3, "looks_good": false, "issues": ["..."], "summary": "...", "created_at": "..." }`.
- **Errors:** `401` unauthenticated; `502` LLM failure; `422` malformed body.

## Operations and enrichment API

These endpoints are backend/operator workflows. No current frontend page calls them except `/seed` and its status endpoint from `frontend/index.html`.

### `GET /health`

Public liveness check. Returns `200` `{ "status": "ok" }`.

### `POST /content-creator-re/bulk-add`

- **Purpose:** Synchronously parse and insert creator URLs into `content_creator_re`.
- **Auth:** None.
- **Body:** `{ "urls": ["https://instagram.com/creator"], "niche": "fitness" }`.
- **Response:** `200` `{ "added": 1, "skipped": 0, "total": 1 }`.
- **Errors:** `422` malformed body; parsing/database failures may produce `500`.
- **Frontend:** `frontend/add-creators.html`.

### `POST /seed` and `GET /seed/status/{job_id}`

- **Purpose:** Start a background brand seed and poll it.
- **Auth:** None in the route implementation.
- **Start body:** `{ "niche": "fitness", "limit": 100, "country": "US", "headquarters": null, "location": null, "operating_area": null, "niche_label": null }`.
- **Start response:** `200` `{ "job_id": "a1b2c3d4", "status": "running", "niche": "fitness", "message": "Seeding 'fitness' started" }`.
- **Status response:** `200` `{ "status": "running|done|error", "niche": "fitness", "inserted": 42, "error": null }`.
- **Errors:** `404` unknown job ID; `422` invalid request; job failures are represented by `status: "error"`.
- **Frontend:** `frontend/index.html` starts the job and polls every two seconds.

### `POST /enrich/signals` and `GET /enrich/signals/status/{job_id}`

- **Purpose:** Run selected signal enrichment steps: `wikidata_socials`, `shopify`, `tranco`, `meta_ads`, and `youtube`.
- **Auth:** None in the route implementation.
- **Start body:** `{ "niche": "fitness", "limit_per_step": 300, "steps": ["shopify", "tranco"] }`; `steps: null` means all steps.
- **Start response:** `{ "job_id": "a1b2c3d4", "status": "running", "message": "Signal enrichment started" }`.
- **Status response:** `{ "status": "running|done|error", "results": {}, "error": null }`.
- **Errors:** `404` unknown job ID; `422` invalid request; job failures appear in the status payload.

### `POST /score/brands` and `GET /score/brands/status/{job_id}`

- **Purpose:** Score enriched brands into `initial_brand_score`.
- **Auth:** None in the route implementation.
- **Start body:** `{ "limit": 500 }`.
- **Start response:** `{ "job_id": "a1b2c3d4", "status": "running", "message": "Brand scoring started" }`.
- **Status response:** `{ "status": "running|done|error", "scored": 42, "error": null }`.
- **Errors:** `404` unknown job ID; `422` invalid body; job failures appear in the status payload.

## Frontend page routes

These serve HTML or static assets rather than JSON APIs:

| Method | Path | Result |
|---|---|---|
| GET | `/` | `frontend/index.html` |
| GET | `/dashboard` | Same dashboard HTML as `/` |
| GET | `/signin` | `frontend/login.html`, `Cache-Control: no-store` |
| GET | `/login` | `301` redirect to `/signin` |
| GET | `/creator-profile` | `frontend/creator-profile.html` |
| GET | `/matches` | `frontend/matches.html` |
| GET | `/add-creators` | `frontend/add-creators.html` |
| GET | `/brand-catalog` | `frontend/brand-catalog.html` |
| GET | `/static/{path}` | File from `frontend/` |
| GET | `/posthog-init.js` | Runtime PostHog loader, or a disabled-analytics warning |

SQLAdmin is mounted at `/admin`. It exposes database model views. Its optional shared passkey gate is controlled by `ADMIN_PASSKEY`; leaving that variable empty leaves admin unauthenticated.

### Frontend page-to-API matrix

| Frontend page | API calls |
|---|---|
| `frontend/login.html` | `GET /auth/me`, `POST /auth/login` |
| `frontend/index.html` | `POST /seed`, `GET /seed/status/{job_id}`, `GET /auth/me`, `POST /auth/logout` |
| `frontend/add-creators.html` | `POST /content-creator-re/bulk-add`, `GET /auth/me`, `POST /auth/logout` |
| `frontend/brand-catalog.html` | `GET /api/brand-catalog/niches`, `GET /api/brand-catalog`, `GET /api/brand-catalog/{brand_id}` |
| `frontend/creator-profile.html` | `POST /creator-profile/me/generate-description`, `GET /creator-sub-niches`, `GET /creator-profile/me`, `PUT /creator-profile/me`, `GET /auth/me` |
| `frontend/matches.html` | `GET /matches/me?limit=20`, `POST /matches/me/refresh?limit=20` |

All six pages also load `/posthog-init.js` for analytics. The API base is implicit because the pages use same-origin relative URLs.

## External services

Secrets and feature flags are configured in `config.py`. These services are called by backend pipeline code, not directly by the browser.

| Service | API / operation | Main call sites | Used for |
|---|---|---|---|
| OpenAI | Chat completions; embeddings (`text-embedding-3-small` by default) | `pipeline/helpers/gpt_llm.py`; creator descriptions, matching enrichment, scoring, pitching, rate intelligence, contract advice, contact ranking and verification | LLM generation, classification, extraction, embeddings |
| Apollo | `/api/v1/mixed_people/api_search`, `/v1/people/match`, optional `/api/v1/webhook_result/{request_id}` | `pipeline/enrichment/apollo_contacts.py` | Find and enrich brand marketing contacts; optional paid phone reveal |
| Hunter.io | `GET https://api.hunter.io/v2/email-verifier` | `pipeline/enrichment/hunter_verify.py` | Verify Apollo email deliverability |
| Apify | Instagram and LinkedIn actors via `ApifyClient` | `pipeline/helpers/apify.py`, `instagram_posts.py`, `instagram_users.py`, `brand_instagram_profile.py`, `linkedin_verify.py` | Scrape Instagram posts/profiles/commenters and LinkedIn data |
| Meta Ad Library | Graph API `https://graph.facebook.com/v21.0`, including `/ads_archive` | `pipeline/enrichment/meta_ads.py` | Resolve Facebook pages and fetch active/historical ads |
| YouTube Data API | Search, videos, channels, comment threads; rotating configured keys | `pipeline/enrichment/youtube_sponsorship.py`, `pipeline/creator_content/youtube_description.py` | Sponsorship detection, audience signals, creator descriptions |
| Wikidata | Search/entity APIs and SPARQL | `pipeline/sources/wikidata.py`, `pipeline/enrichment/wikidata_socials.py`, `pipeline/enrichment_re/brand_wikidata_lookup.py` | Brand seeding, entity identity, websites and social handles |
| Wikipedia | Search/category APIs and Wikidata entity lookup | `pipeline/sources/wikipedia.py` | Legacy/alternate brand discovery and filtering |
| SearXNG | `${SEARXNG_URL}/search?format=json` | `pipeline/enrichment_re/brand_instagram_profile.py` | Fallback website discovery; defaults to local `http://localhost:8080` |
| Tranco | `https://tranco-list.eu/top-1m.csv.zip` | `pipeline/enrichment/tranco.py` | Domain popularity/rank enrichment |
| Brand websites | HTTP fetches of home/about paths | `pipeline/enrichment/shopify_detect.py` | Shopify/WooCommerce detection, social links, descriptions |
| PostHog | Browser loader from configured `POSTHOG_HOST` | `/posthog-init.js` in `api/app.py`; all six HTML pages | Page analytics and identify/reset calls |

External calls can be triggered indirectly by `/seed`, `/enrich/signals`, `/score/brands`, profile description generation, profile save background work, matching, pitching, and advisory endpoints. Do not expose service keys in frontend code.

## Frontend integration flows

### Authentication

1. Load `/signin`.
2. Optionally call `GET /auth/me` to reuse an existing cookie session.
3. Submit `POST /auth/login` with email/password and `credentials: "include"`.
4. Redirect to `return_to` or the dashboard. The response also contains the profile.
5. Call `POST /auth/logout` when signing out; clear analytics identity with the page's PostHog helper.

### Creator onboarding

1. `creator-profile.html` collects platform, handles, follower counts, niches, exclusions, descriptions, and audience percentages.
2. For generated descriptions, call `POST /creator-profile/me/generate-description` once per selected platform.
3. Save with `PUT /creator-profile/me`.
4. The response immediately includes the synchronously calculated tier. Tags/embedding refresh asynchronously.
5. If unauthenticated, the page stores the pending form locally, redirects to `/signin`, then retries the save after login.

### Brand matching

1. Navigate to `/matches`.
2. Call `GET /matches/me?limit=20` to render ranked matches.
3. Call `POST /matches/me/refresh?limit=20` for an explicit refresh control. Today it recomputes the same live query.
4. A future detail view can call authenticated `GET /brands/{brand_id}` for contact, activity, and tier-fit data.

### Profile management and creator actions

- Profile editing uses `GET /creator-profile/me` followed by `PUT /creator-profile/me`.
- A future brand-save workflow should use `POST /saved-brands`, `GET /saved-brands/me`, and `DELETE /saved-brands/{brand_id}`.
- A future pitching workflow should load brand detail, submit `POST /pitches`, and list progress with `GET /pitches/me`.
- Dashboard cards should use `GET /dashboard/me`.
- Rate and contract tools use `POST /rate-intelligence` and `POST /contract-advice`; both may take several seconds because of live LLM work.

## Gaps and maintenance flags

- **Unused by bundled frontend:** `/brand-niches`, `/brand-niche-tags`, `/brands/{brand_id}`, all saved-brand routes, pitch routes, `/dashboard/me`, advisory routes, `/enrich/signals`, `/score/brands`, and their status endpoints. They are implemented API surface, not necessarily dead code.
- **Potential duplicate:** `/matches/me/refresh` and `GET /matches/me` currently compute the same live result. Keep both only if the explicit refresh contract is planned.
- **Compatibility routes:** `/dashboard` duplicates `/`; `/login` is a permanent-style compatibility redirect to `/signin`.
- **Catalog placeholder:** `/api/brand-catalog/{brand_id}` returns the same hardcoded three ad examples for every brand. Treat `meta_ads` as demo data until this is replaced with a real per-brand query.
- **Niche source:** `/brand-niches` reads only distinct non-empty values from `content_creator_re.niche`; the profile page still hardcodes its primary-niche options and should consume this endpoint when that UI is updated.
- **Unauthenticated operator routes:** `/seed`, `/enrich/signals`, `/score/brands`, and `/content-creator-re/bulk-add` have no auth dependency. Put them behind an operator auth boundary before exposing the service outside a trusted network.
- **In-memory jobs:** Polling IDs stop working after process restart and can diverge across multiple Uvicorn workers. Use a durable job store or enforce one worker if these workflows become production-critical.
- **Missing pagination/history:** Pitches have no pagination, and rate estimates/contract reviews have no history endpoints.
- **Status is open-ended:** Pitch status is free text in the database; clients should not assume only `proposal_sent` exists.
- **No OpenAPI documentation for page routes:** HTML/static routes intentionally use `include_in_schema=False`; this document includes them manually.