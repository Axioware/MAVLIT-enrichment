"""
pipeline/migration_settings.py

Where `python -m pipeline.migrate` gets its database credentials, kept apart from the API's configuration on purpose:
the API (config.py, api/) never reads MIGRATION_DATABASE_URL, so the schema owner's password does not have to sit in
the API's environment (the systemd EnvironmentFile of mavlit.service).

Where the migration URL comes from, first match wins:
  1. --env-file PATH, or the MIGRATION_ENV_FILE variable: a dedicated file that defines MIGRATION_DATABASE_URL.
  2. the MIGRATION_DATABASE_URL variable of the migration process itself (exported by a deploy step).
  3. the default file .env.migrate in the project root, when it exists.
  4. none of these: the runtime DATABASE_URL (local development; a restricted production role will lack the rights).

A file is read without touching os.environ, and must be readable only by its owner (chmod 600) and owned by that user
or by root. The URL is checked before anything connects to a database. Nothing here ever includes a URL or a password
in an error message; redact() removes them from messages produced by other libraries.

Importing this module imports no database code and opens no connection.
"""

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env.migrate"
URL_VARIABLE = "MIGRATION_DATABASE_URL"
ENV_FILE_VARIABLE = "MIGRATION_ENV_FILE"
LOCK_TIMEOUT_VARIABLE = "MIGRATION_LOCK_TIMEOUT_MS"
GRANT_ROLE_VARIABLE = "MIGRATION_GRANT_ROLE"
DEFAULT_LOCK_TIMEOUT_MS = 10_000   # how long a standalone migration waits for any one lock
MAX_LOCK_TIMEOUT_MS = 3_600_000


class MigrationConfigError(RuntimeError):
    """The migration settings are missing or unusable. The message never contains a URL or a password."""


@dataclass(frozen=True)
class MigrationTarget:
    url: str | None   # None = use the runtime engine (DATABASE_URL)
    source: str       # a description for logs: a variable name or a file path, never a secret
    file_values: dict | None = None   # the other settings of the migration env file (e.g. MIGRATION_GRANT_ROLE), if one was used


def _check_env_file(path: Path) -> None:
    try:
        info = path.stat()
    except FileNotFoundError:
        raise MigrationConfigError(f"The migration env file {path} does not exist") from None
    if not stat.S_ISREG(info.st_mode):
        raise MigrationConfigError(f"The migration env file {path} is not a regular file")
    if os.name == "posix":
        if info.st_mode & 0o077:
            raise MigrationConfigError(f"The migration env file {path} must not be readable by group or others (chmod 600 {path})")
        if info.st_uid not in (0, os.geteuid()):
            raise MigrationConfigError(f"The migration env file {path} must be owned by root or by the user running the migration")


def _read_file(path: Path) -> dict:
    _check_env_file(path)
    values = dict(dotenv_values(path))
    if not (values.get(URL_VARIABLE) or "").strip():
        raise MigrationConfigError(f"The migration env file {path} does not define {URL_VARIABLE}")
    return values


def validate_url(raw: str, where: str = URL_VARIABLE) -> None:
    """Check a migration URL without connecting. Raises MigrationConfigError, never echoing the value."""
    try:
        url = make_url(raw)
    except ArgumentError:
        raise MigrationConfigError(
            f"{where} is not a valid database URL (expected postgresql+psycopg2://user:password@host:port/database)"
        ) from None
    if not url.drivername.startswith("postgresql"):
        raise MigrationConfigError(f"{where} must be a PostgreSQL URL (it starts with {url.drivername!r})")
    if not url.database:
        raise MigrationConfigError(f"{where} does not name a database")


def resolve_migration_target(env_file: str | None = None, environ: dict | None = None) -> MigrationTarget:
    """Pick (and validate) the database the migrations will use. Reads files and variables only; connects to nothing."""
    environ = os.environ if environ is None else environ
    explicit = env_file or (environ.get(ENV_FILE_VARIABLE) or "").strip() or None
    values = None
    if explicit:
        values, source = _read_file(Path(explicit)), f"the migration env file {explicit}"
    elif (environ.get(URL_VARIABLE) or "").strip():
        source = f"the {URL_VARIABLE} variable"
    elif DEFAULT_ENV_FILE.exists():
        values, source = _read_file(DEFAULT_ENV_FILE), f"the migration env file {DEFAULT_ENV_FILE}"
    else:
        return MigrationTarget(url=None, source="the runtime DATABASE_URL (no migration URL is configured)")
    raw = values[URL_VARIABLE].strip() if values is not None else environ[URL_VARIABLE].strip()
    validate_url(raw, where=f"{URL_VARIABLE} in {source}")
    return MigrationTarget(url=raw, source=source, file_values=values)


def resolve_lock_timeout_ms(cli_value: str | int | None = None, environ: dict | None = None) -> int | None:
    """Milliseconds the standalone migration waits for a lock; None means no limit (0 turns the limit off)."""
    environ = os.environ if environ is None else environ
    raw = cli_value if cli_value is not None else (environ.get(LOCK_TIMEOUT_VARIABLE) or "").strip() or None
    if raw is None:
        return DEFAULT_LOCK_TIMEOUT_MS
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise MigrationConfigError(f"{LOCK_TIMEOUT_VARIABLE} / --lock-timeout-ms must be a whole number of milliseconds (0 = no limit)") from None
    if value < 0 or value > MAX_LOCK_TIMEOUT_MS:
        raise MigrationConfigError(f"{LOCK_TIMEOUT_VARIABLE} / --lock-timeout-ms must be between 0 and {MAX_LOCK_TIMEOUT_MS}")
    return value or None


def resolve_grant_role(cli_value: str | None = None, environ: dict | None = None, file_values: dict | None = None) -> str | None:
    """
    The runtime role to (re)grant after the migrations; None = grant nothing. First match wins: --grant-role, the
    MIGRATION_GRANT_ROLE variable of the migration process, MIGRATION_GRANT_ROLE in the migration env file (a role name is not
    a secret, but it belongs with the migration settings and not in the API's environment).
    """
    environ = os.environ if environ is None else environ
    raw = cli_value if cli_value is not None else ((environ.get(GRANT_ROLE_VARIABLE) or "") or ((file_values or {}).get(GRANT_ROLE_VARIABLE) or ""))
    role = raw.strip()
    if not role:
        return None
    if not re.match(r"^[a-z_][a-z0-9_]{0,62}$", role):
        raise MigrationConfigError(f"{GRANT_ROLE_VARIABLE} / --grant-role must be a plain lower-case role name (letters, digits, underscore)")
    return role


def build_engine(raw_url: str) -> Engine:
    """A dedicated, non-pooling engine for the migration URL. Creating it does not connect."""
    try:
        return create_engine(raw_url, poolclass=NullPool, pool_pre_ping=True)
    except Exception as exc:   # e.g. a missing driver; the exception text could echo the URL, so only its type is kept
        raise MigrationConfigError(f"Could not create a database engine from the migration URL ({type(exc).__name__})") from None


def redact(message: str, *urls: str | None) -> str:
    """Remove the given URLs and their passwords from a message written by another library."""
    for raw in urls:
        if not raw:
            continue
        message = message.replace(raw, "<database url>")
        try:
            password = make_url(raw).password
        except ArgumentError:
            continue
        if password:
            message = message.replace(password, "***")
    return message
