import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set in environment")

APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")

# Signal enrichment
GOOGLE_API_KEY    = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_CX         = os.getenv("GOOGLE_CX", "")       # Custom Search Engine ID
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
YOUTUBE_API_KEY   = os.getenv("YOUTUBE_API_KEY", "")
YOUTUBE_API_KEY_1 = os.getenv("YOUTUBE_API_KEY_1", "")
APIFY_TOKEN       = os.getenv("APIFY_TOKEN", "")

# LLM verification (Claude) — set ENABLE_LLM=true to activate
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
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