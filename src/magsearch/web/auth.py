import hashlib

import bcrypt


def hash_password(plain: str) -> str:
    """Hash a password with bcrypt.

    Bcrypt has a hard 72-byte input limit, so we pre-hash long passwords with
    SHA-256 (a common idiom that avoids silent truncation) before bcrypting.
    """
    pre = _prepare(plain)
    return bcrypt.hashpw(pre, bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prepare(plain), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def _prepare(plain: str) -> bytes:
    raw = plain.encode("utf-8")
    if len(raw) > 72:
        # Pre-hash to avoid bcrypt's 72-byte truncation.
        return hashlib.sha256(raw).hexdigest().encode("ascii")
    return raw


def normalize_username(raw: str) -> str:
    return raw.strip().lower()
