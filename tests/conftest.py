"""Secrets the application now refuses to start without (config.py); fixed test values, set before any test module imports the app."""
import os

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-0123456789-abcdefghijklmnop")
os.environ.setdefault("ADMIN_SESSION_SECRET", "test-admin-session-secret-0123456789-abcdefgh")

# Cookie/environment behaviour must not depend on the developer's local .env (load_dotenv never overrides a variable that is already set).
for _name, _value in (("ENVIRONMENT", "development"), ("COOKIE_SAMESITE", "lax"), ("ACCESS_TOKEN_DAYS", "30")):
    os.environ[_name] = _value
os.environ.pop("COOKIE_DOMAIN", None)
