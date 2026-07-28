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
APIFY_TOKEN       = os.getenv("APIFY_TOKEN", "")

# LLM verification (Claude) — set ENABLE_LLM=true to activate
ENABLE_LLM        = os.getenv("ENABLE_LLM", "false").strip().lower() == "true"

# Demographics classification (Mistral)
MISTRAL_API_KEY   = os.getenv("MISTRAL_API_KEY", "")

# Instagram post LLM creator verification — independent of ENABLE_LLM
# When true: taggedUsers + mentions are also sent to LLM (in addition to coauthorProducers)
# When false: only coauthorProducers is LLM-checked (always on)
ENABLE_INSTA_LLM  = os.getenv("ENABLE_INSTA_LLM", "false").strip().lower() == "true"

# Google OAuth + JWT auth
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID") or os.getenv("CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv("CLIENT_SECRET", "")
OAUTH_REDIRECT_URI   = os.getenv("OAUTH_REDIRECT_URI", "http://127.0.0.1:8000/auth/google/callback")

JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET is not set in environment")

# Controls cookie Secure flag + HSTS header. Set ENVIRONMENT=production when
# deploying behind HTTPS — cookies must never be sent unencrypted in prod.
ENVIRONMENT   = os.getenv("ENVIRONMENT", "development").strip().lower()
IS_PRODUCTION = ENVIRONMENT == "production"
