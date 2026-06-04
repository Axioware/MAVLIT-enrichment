import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set in environment")

APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "")
HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")

ENRICH_BATCH_SIZE = int(os.getenv("ENRICH_BATCH_SIZE", "50"))
CONTACTS_BATCH_SIZE = int(os.getenv("CONTACTS_BATCH_SIZE", "50"))
VERIFY_BATCH_SIZE = int(os.getenv("VERIFY_BATCH_SIZE", "80"))
