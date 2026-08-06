import bcrypt


def hash_password(plain_password: str) -> str:
    """Hash a plain-text password for storage — never store plain text."""
    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Check a plain-text password against a stored bcrypt hash."""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed/non-bcrypt hash stored — treat as no match rather than raising.
        return False
