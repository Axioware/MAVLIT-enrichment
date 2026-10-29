"""
api/settings.py

Settings of the API process that concern the database migrations. Kept out of config.py on purpose: config.py is
imported by every pipeline script and by `python -m pipeline.migrate`, and none of them should depend on (or fail
because of) RUN_MIGRATIONS.

The API never reads MIGRATION_DATABASE_URL. The schema owner's credentials belong in a separate file used only by the
migration step (see MIGRATIONS.md); if one is found in the API's environment it is reported and removed from it.
"""

import logging
import os

import config  # noqa: F401  (importing it loads .env into the environment first)

logger = logging.getLogger(__name__)


def parse_run_migrations(value: str | None) -> bool:
    """RUN_MIGRATIONS: whether the API runs the migrations at startup. Unset or empty keeps the old behaviour (on)."""
    text = (value or "").strip().lower()
    if not text:
        return True
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise RuntimeError(f"RUN_MIGRATIONS must be one of 1/true/yes/on or 0/false/no/off (or unset), got {value!r}")


RUN_MIGRATIONS = parse_run_migrations(os.getenv("RUN_MIGRATIONS"))


def drop_migration_credentials() -> bool:
    """If MIGRATION_DATABASE_URL reached the API's environment, say so (never its value) and remove it. True if it was there."""
    if not (os.environ.get("MIGRATION_DATABASE_URL") or "").strip():
        return False
    os.environ.pop("MIGRATION_DATABASE_URL", None)
    logger.warning(
        "MIGRATION_DATABASE_URL is set in the API's environment. The API never uses it; remove it from .env / the service "
        "environment and keep it in the migration-only env file (see MIGRATIONS.md)."
    )
    return True
