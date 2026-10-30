import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set in environment")

APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")

# Apollo phone-number reveal costs an extra ~8 credits per person (confirmed
# from live Apollo responses) on top of the email enrich cost — off by
# default so it's an opt-in cost, not an automatic one. When enabled,
# pipeline/enrichment/apollo_contacts.py polls Apollo's async result
# endpoint for the number; no public webhook/tunnel is needed for this.
ENABLE_APOLLO_PHONE_REVEAL = os.getenv("ENABLE_APOLLO_PHONE_REVEAL", "false").strip().lower() == "true"

# Signal enrichment
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
YOUTUBE_API_KEY   = os.getenv("YOUTUBE_API_KEY", "")
YOUTUBE_API_KEY_1 = os.getenv("YOUTUBE_API_KEY_1", "")
YOUTUBE_API_KEY_2 = os.getenv("YOUTUBE_API_KEY_2", "")
YOUTUBE_API_KEY_3 = os.getenv("YOUTUBE_API_KEY_3", "")
YOUTUBE_API_KEY_4 = os.getenv("YOUTUBE_API_KEY_4", "")
YOUTUBE_API_KEY_5 = os.getenv("YOUTUBE_API_KEY_5", "")
YOUTUBE_API_KEY_6 = os.getenv("YOUTUBE_API_KEY_6", "")
YOUTUBE_API_KEY_7 = os.getenv("YOUTUBE_API_KEY_7", "")
YOUTUBE_API_KEY_8 = os.getenv("YOUTUBE_API_KEY_8", "")
YOUTUBE_API_KEY_9 = os.getenv("YOUTUBE_API_KEY_9", "")
YOUTUBE_API_KEY_10 = os.getenv("YOUTUBE_API_KEY_10", "")
YOUTUBE_API_KEY_11 = os.getenv("YOUTUBE_API_KEY_11", "")
YOUTUBE_API_KEY_12 = os.getenv("YOUTUBE_API_KEY_12", "")
APIFY_TOKEN       = os.getenv("APIFY_TOKEN", "")

# Jina AI Reader (https://r.jina.ai) — fallback page fetcher used by
# pipeline/enrichment/shopify_detect.py when a direct fetch is blocked or
# errors. Optional: works without a key on the free tier (~20 req/min).
JINA_API_KEY = os.getenv("JINA_API_KEY") or os.getenv("JINAAI_API_KEY", "")   # either name works in .env

# Firecrawl (https://firecrawl.dev) — temporary replacement for Jina as the
# rendered-page fallback fetcher (pipeline/helpers/http.py). When
# FIRECRAWL_API_KEY is set it is used instead of Jina; set FETCH_FALLBACK=jina
# (or remove the key) to switch back.
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")
FIRECRAWL_RATE_PER_MIN = int(os.getenv("FIRECRAWL_RATE_PER_MIN", "10"))   # free plan: 10 requests/minute
FETCH_FALLBACK = (os.getenv("FETCH_FALLBACK") or ("firecrawl" if FIRECRAWL_API_KEY else "jina")).strip().lower()

# Hunter.io Email Verifier — pipeline/enrichment/hunter_verify.py, checks
# deliverability of emails Apollo already found and saved in brand_contacts.
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")

# Local SearXNG instance — fallback website discovery in
# pipeline/enrichment_re/brand_instagram_profile.py when a brand's Instagram
# bio link doesn't resolve to a website. Requires JSON output enabled on the
# SearXNG instance itself (search.formats in its settings.yml must include
# "json" — disabled by default). No API key/quota, unlike the Google Custom
# Search API this replaced.
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8080")

# Proxies for pipeline/enrichment_re/facebook_branded_content_checker.py —
# comma-separated "ip:port:user:pass" (or "ip:port"); one is used per batch of
# checks, rotating. Empty = connect directly.
FACEBOOK_PROXIES = os.getenv("FACEBOOK_PROXIES", "")

# LLM verification (Claude) — set ENABLE_LLM=true to activate
ENABLE_LLM        = os.getenv("ENABLE_LLM", "false").strip().lower() == "true"

# LLM classification/extraction + embeddings (OpenAI, gpt-6-luna + text-embedding-3-small)
OPENAI_KEY = os.getenv("OPENAI_KEY", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

# Frontend analytics (PostHog). The project token is a public write-only key
# (safe to ship to the browser — this is how PostHog's JS snippet is meant
# to be used), served to the frontend via GET /posthog-init.js rather than
# hardcoded into the static HTML so it stays configurable per environment.
POSTHOG_PROJECT_TOKEN = os.getenv("POSTHOG_PROJECT_TOKEN", "")
POSTHOG_HOST          = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com")

# Origins of separate frontend apps (different domain than this API) allowed
# to call this API cross-origin with cookies via CORS. Comma-separated, no
# trailing slash, e.g. "https://app.example.com,http://localhost:5173"
FRONTEND_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("FRONTEND_ORIGINS", "").split(",") if o.strip()]

# JWT auth (login is email/password against creator_profiles.password_hash
# — see api/auth.py and pipeline/db.py's CreatorProfile)
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET is not set in environment")
MIN_SECRET_LENGTH = 32
if len(JWT_SECRET) < MIN_SECRET_LENGTH:   # never print the value
    raise RuntimeError(f"JWT_SECRET must be at least {MIN_SECRET_LENGTH} characters (e.g. python -c 'import secrets; print(secrets.token_urlsafe(48))')")

# Signs the /admin session cookie (SQLAdmin). A different secret from JWT_SECRET on purpose: one leak must not forge both user sessions and
# admin sessions. Required, 32+ characters.
ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET")
if not ADMIN_SESSION_SECRET:
    raise RuntimeError("ADMIN_SESSION_SECRET is not set in environment")
if len(ADMIN_SESSION_SECRET) < MIN_SECRET_LENGTH:
    raise RuntimeError(f"ADMIN_SESSION_SECRET must be at least {MIN_SECRET_LENGTH} characters")
if ADMIN_SESSION_SECRET == JWT_SECRET:
    raise RuntimeError("ADMIN_SESSION_SECRET must differ from JWT_SECRET")

# /admin (sqladmin) gate — a single shared passkey, not a per-person account.
# Left empty, /admin is unprotected — set this before deploying anywhere
# reachable outside your own machine. See AdminAuth in api/app.py.
ADMIN_PASSKEY = os.getenv("ADMIN_PASSKEY", "")
# Optional separate credential for the operator endpoints (/seed, /enrich/signals, /score/brands, /content-creator-re/bulk-add), sent as
# "Authorization: Bearer <OPERATOR_PASSKEY>". Unset: ADMIN_PASSKEY keeps working there as before. Set: only this one does.
OPERATOR_PASSKEY = os.getenv("OPERATOR_PASSKEY", "")

# Controls cookie Secure flag + HSTS header. Set ENVIRONMENT=production when
# deploying behind HTTPS — cookies must never be sent unencrypted in prod.
ENVIRONMENT   = os.getenv("ENVIRONMENT", "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT == "production"

MIN_PASSKEY_LENGTH_PRODUCTION = 16
for _name, _value in (("ADMIN_PASSKEY", ADMIN_PASSKEY), ("OPERATOR_PASSKEY", OPERATOR_PASSKEY)):
    if IS_PRODUCTION and _value.strip() and len(_value) < MIN_PASSKEY_LENGTH_PRODUCTION:   # an unset / blank one keeps its endpoints closed
        raise RuntimeError(f"{_name} must be at least {MIN_PASSKEY_LENGTH_PRODUCTION} characters when ENVIRONMENT=production")

# Cookie attributes of the login cookie (api/auth.py). The default is Lax, which is right once the frontend and the API are on the same site
# (e.g. app.mavlit.com and api.mavlit.com). A frontend on a different site needs COOKIE_SAMESITE=none (production only: it requires Secure).
# COOKIE_DOMAIN is unset by default (a host-only cookie); set it, e.g. ".mavlit.com", only if subdomains must share the login.
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "lax").strip().lower() or "lax"
if COOKIE_SAMESITE not in ("lax", "strict", "none"):
    raise RuntimeError("COOKIE_SAMESITE must be lax, strict or none")
if COOKIE_SAMESITE == "none" and not IS_PRODUCTION:
    raise RuntimeError("COOKIE_SAMESITE=none needs ENVIRONMENT=production (browsers drop SameSite=None cookies that are not Secure)")
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN", "").strip() or None
