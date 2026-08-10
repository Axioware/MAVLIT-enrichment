# API Reference — Creator Tools

Backend reference for the frontend build: brand detail, saved brands, pitching, dashboard, and the two LLM advisory tools (rate intelligence, contract advice). Auth endpoints are included since every route below requires a logged-in session.

## Auth model

- Cookie-based JWT, **not** a bearer token. `POST /auth/login` sets an `httpOnly` cookie (`access_token`) on success — the frontend never reads or stores the token itself.
- Every `fetch()` call to this API must include `credentials: 'include'` so the cookie is sent, e.g.:
  ```js
  fetch(`${API_BASE}/dashboard/me`, { credentials: 'include' })
  ```
- If the API and frontend are on different origins, the frontend origin must be in the backend's `FRONTEND_ORIGINS` env var (CORS), and requests will be treated as cross-site (`SameSite=None; Secure` in production).
- Every endpoint below returns **401** if the `access_token` cookie is missing, invalid, expired, or the user is inactive.
- Session length: 7 days from login.

### `POST /auth/login`
Body:
```json
{ "email": "creator@example.com", "password": "..." }
```
Response `200` — full creator profile (see `CreatorProfileResponse` in `/creator-profile/me` below). Sets the `access_token` cookie.
Errors: `401` invalid email/password, `403` account deactivated.

### `GET /auth/me`
Response `200`: `{ "id": int, "email": str, "name": str | null }`

### `POST /auth/logout`
Clears the cookie. Response `200`: `{ "message": "Logged out" }`

### `GET /creator-profile/me` / `PUT /creator-profile/me`
Full creator profile CRUD — not new, but the `follower_count`, `primary_platform`, etc. set here are what rate-intelligence and matching read. Not detailed further here — see existing frontend integration in `frontend/creator-profile.html` for the field list.

---

## 1. Brand detail

### `GET /brands/{brand_id}`
Auth required. Returns `404` if the brand doesn't exist.

**Response `200`:**
```json
{
  "brand_id": 1,
  "name": "8D Creative",
  "short_bio": "Short one-line description (Wikidata-sourced) or null",
  "description": "Longer scraped about-page text, or null — falls back to short_bio's source if no long description exists",
  "niche": "music",
  "creator_tier_fit": {
    "typical_tier": "micro",          // "nano" | "micro" | "macro" | "mega" | null
    "followers_low": 12000,           // null if no collaborator data yet
    "followers_high": 250000
  },
  "last_partnership_date": "2025-11-02",   // ISO date, or null if no sponsorship history found
  "meta_ads_active": true,                  // null if unknown/not yet computed
  "platform_signals": {
    "instagram": "moderate",   // "not_available" | "limited" | "moderate" | "strong"
    "youtube": "strong"
  },
  "sponsorship_activity_score": 62.5,   // 0-100, null if not yet computed
  "top_contact": {
    "name": "Jane Doe",
    "email": "jane@brand.com",
    "title": "Head of Marketing"
  }   // or null if no verified marketing contact found yet
}
```

Notes for the UI:
- Every field can be `null` — brand signal computation is a background pipeline, not everything is filled in for every brand. Render placeholders/"Not available yet" rather than assuming presence.
- `platform_signals` is per-platform, always one of the 4 enum strings above (never null) — safe to map directly to a badge/chip component.
- `top_contact` being `null` means "no verified contact found" — the frontend should probably still let the creator generate a pitch (it just won't be addressed to anyone specific).

---

## 2. Saved brands

### `POST /saved-brands`
Auth required. Idempotent — saving an already-saved brand is a no-op, not an error.
Body:
```json
{ "brand_id": 1 }
```
Response `201`: `{ "brand_id": 1, "saved": true }`
Errors: `404` if brand_id doesn't exist.

### `GET /saved-brands/me`
Auth required. Response `200` — array, newest-saved first:
```json
[
  { "brand_id": 1, "name": "8D Creative", "niche": "music", "short_bio": "...", "saved_at": "2026-08-10 12:00:00+00" }
]
```

### `DELETE /saved-brands/{brand_id}`
Auth required. Response `200`: `{ "brand_id": 1, "saved": false }`
Errors: `404` if the brand wasn't saved.

---

## 3. Pitches

### `POST /pitches`
Auth required. Generates a personalized pitch email via LLM (using the creator's profile, the brand's details, and its top contact) and immediately saves it with `status: "proposal_sent"`.

**This call can take several seconds** (live LLM call) — show a loading state, don't treat a slow response as an error.

Body — **existing brand**:
```json
{
  "is_custom": false,
  "brand_id": 1,
  "story": "Why this creator wants to work with this brand specifically",
  "product_reference": "The product/line they're interested in",       // optional
  "past_brand_partnership": "Relevant past sponsorships",               // optional
  "content_link": "https://instagram.com/p/..."                        // optional
}
```
Body — **custom brand** (not in the database):
```json
{
  "is_custom": true,
  "custom_brand_name": "Some Brand Not In Our DB",
  "story": "...",
  "product_reference": null,
  "past_brand_partnership": null,
  "content_link": null
}
```
Validation: `is_custom: true` requires `custom_brand_name` and `brand_id` must be omitted/null. `is_custom: false` requires `brand_id`.

**Response `200`:**
```json
{
  "id": 42,
  "brand_id": 1,
  "brand_name": "8D Creative",
  "is_custom": false,
  "story": "...",
  "product_reference": "...",
  "past_brand_partnership": "...",
  "content_link": "...",
  "contact_name": "Jane Doe",
  "contact_email": "jane@brand.com",
  "pitch_text": "Hi Jane,\n\nI've been a fan of...",
  "status": "proposal_sent",
  "created_at": "2026-08-10 12:00:00+00"
}
```
Errors: `400` bad combination of `is_custom`/`brand_id`/`custom_brand_name`, `404` brand_id not found, `502` LLM generation failed (nothing is saved in this case — safe to let the user retry).

### `GET /pitches/me`
Auth required. Response `200` — array of the same shape as above, newest first. This is the creator's pitch history/tracker list.

`status` is free text (not a fixed enum in the DB) — currently only `"proposal_sent"` is ever set by the backend. Treat any other value the same way (design for a future status like `"in_discussion"` / `"closed_won"` / `"closed_lost"` / `"declined"` without assuming today's UI covers them).

---

## 4. Dashboard

### `GET /dashboard/me`
Auth required. Response `200`:
```json
{
  "brand_matches": 37,
  "active_deals": 3,
  "saved_brands": 8,
  "verified_contacts": 5
}
```
- `brand_matches` — count of the creator's current Stage-3 match results (capped at 100).
- `active_deals` — count of the creator's pitches not in a terminal status (`closed_won`/`closed_lost`/`declined` — none exist yet in practice, so today this equals total pitch count).
- `saved_brands` — count from the saved-brands list above.
- `verified_contacts` — count of verified (real email) contacts, scoped to brands this creator has **saved or pitched** — not a platform-wide total.

---

## 5. Rate intelligence

### `POST /rate-intelligence`
Auth required. Live LLM call — same latency caveat as pitch generation. Every call is saved (there's no history-fetch endpoint for this yet, only the direct response).

Body:
```json
{
  "brand_id": 1,
  "platform": "instagram",
  "deliverable_type": "1 feed post + 3 stories",
  "exclusivity": "category exclusivity for 30 days",   // optional, free text
  "usage": "organic only, no paid amplification",       // optional, free text
  "duration_months": 1                                    // optional
}
```
Response `200`:
```json
{
  "id": 7,
  "rate_min": 800,
  "rate_max": 1500,
  "currency": "USD",
  "reasoning": "Based on the creator's micro tier and the brand's moderate sponsorship activity...",
  "created_at": "2026-08-10 12:00:00+00"
}
```
Errors: `404` brand_id not found, `502` LLM call failed (not saved).

---

## 6. Contract advice

### `POST /contract-advice`
Auth required. Live LLM call.

Body:
```json
{ "contract_text": "full pasted contract text..." }
```
Response `200`:
```json
{
  "id": 3,
  "looks_good": false,
  "issues": [
    "Usage rights are granted in perpetuity with no additional compensation clause",
    "No kill fee or cancellation terms specified"
  ],
  "summary": "Mostly standard terms, but the perpetual usage grant and missing cancellation clause are worth pushing back on.",
  "created_at": "2026-08-10 12:00:00+00"
}
```
Errors: `502` LLM call failed (not saved).
Not legal advice — the summary/issues are plain-language flags for the creator to review, worth a small disclaimer in the UI.

---

## Error shape (all endpoints)

FastAPI's default error body:
```json
{ "detail": "human-readable message" }
```
Common codes across all endpoints above: `401` not logged in, `400` bad request body, `404` referenced brand/resource not found, `502` upstream LLM call failed (safe to retry, nothing was written).
