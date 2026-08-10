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
APIFY_TOKEN       = os.getenv("APIFY_TOKEN", "")

# LLM verification (Claude) — set ENABLE_LLM=true to activate
ENABLE_LLM        = os.getenv("ENABLE_LLM", "false").strip().lower() == "true"

# LLM classification/extraction + embeddings (OpenAI, gpt-5-mini + text-embedding-3-small)
OPENAI_KEY = os.getenv("OPENAI_KEY", "")

# Frontend analytics (PostHog). The project token is a public write-only key
# (safe to ship to the browser — this is how PostHog's JS snippet is meant
# to be used), served to the frontend via GET /posthog-init.js rather than
# hardcoded into the static HTML so it stays configurable per environment.
POSTHOG_PROJECT_TOKEN = os.getenv("POSTHOG_PROJECT_TOKEN", "")
POSTHOG_HOST          = os.getenv("POSTHOG_HOST", "https://us.i.posthog.com")

# Instagram post LLM creator verification — independent of ENABLE_LLM
# When true: taggedUsers + mentions are also sent to LLM (in addition to coauthorProducers)
# When false: only coauthorProducers is LLM-checked (always on)
ENABLE_INSTA_LLM  = os.getenv("ENABLE_INSTA_LLM", "false").strip().lower() == "true"

# Origins of separate frontend apps (different domain than this API) allowed
# to call this API cross-origin with cookies via CORS. Comma-separated, no
# trailing slash, e.g. "https://app.example.com,http://localhost:5173"
FRONTEND_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("FRONTEND_ORIGINS", "").split(",") if o.strip()]

# JWT auth (login is email/password against creator_profiles.password_hash
# — see api/auth.py and pipeline/db.py's CreatorProfile)
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET is not set in environment")

# /admin (sqladmin) gate — a single shared passkey, not a per-person account.
# Left empty, /admin is unprotected — set this before deploying anywhere
# reachable outside your own machine. See AdminAuth in api/app.py.
ADMIN_PASSKEY = os.getenv("ADMIN_PASSKEY", "")

# Controls cookie Secure flag + HSTS header. Set ENVIRONMENT=production when
# deploying behind HTTPS — cookies must never be sent unencrypted in prod.
ENVIRONMENT   = os.getenv("ENVIRONMENT", "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT == "production"
