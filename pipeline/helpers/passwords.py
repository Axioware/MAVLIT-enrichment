import os

import bcrypt

MAX_PASSWORD_BYTES = 72   # bcrypt ignores everything after 72 bytes (bcrypt 5 refuses such a password)


def bcrypt_rounds() -> int:
    """The work factor for new hashes (BCRYPT_ROUNDS, default 12). A hash made with fewer rounds is upgraded the next time its owner logs in."""
    raw = (os.environ.get("BCRYPT_ROUNDS") or "").strip()
    rounds = int(raw) if raw else 12
    if not 10 <= rounds <= 16:
        raise ValueError("BCRYPT_ROUNDS must be between 10 and 16")
    return rounds


def hash_password(plain_password: str) -> str:
    """Hash a plain-text password for storage - never store plain text. Raises ValueError (with a message fit to show) when it is too long."""
    encoded = plain_password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ValueError(f"The password is too long: at most {MAX_PASSWORD_BYTES} bytes (about {MAX_PASSWORD_BYTES} plain characters) are supported.")
    return bcrypt.hashpw(encoded, bcrypt.gensalt(rounds=bcrypt_rounds())).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Check a plain-text password against a stored bcrypt hash."""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed/non-bcrypt hash stored (or a password over 72 bytes) - treat as no match rather than raising.
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored bcrypt hash was made with fewer rounds than bcrypt_rounds() (or is not a readable bcrypt hash)."""
    try:
        return int(password_hash.split("$")[2]) < bcrypt_rounds()
    except (IndexError, ValueError):
        return False   # not a bcrypt hash we can read: leave it alone
