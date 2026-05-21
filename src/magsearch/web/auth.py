import hashlib
import secrets
from datetime import datetime

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from magsearch.models import User

LOCAL_ADMIN_USERNAME = "local"


def ensure_local_admin(db: Session) -> User:
    """Return the desktop-mode 'local' admin user, creating it if missing.

    Called when auth_enabled is False so that route handlers depending on
    `require_admin` still resolve. The password hash is for an unguessable
    random string — there is no intended login path; the user only exists to
    populate `request.state.current_user`.
    """
    user = db.scalar(select(User).where(User.username == LOCAL_ADMIN_USERNAME))
    if user is not None:
        return user
    user = User(
        username=LOCAL_ADMIN_USERNAME,
        password_hash=hash_password(secrets.token_urlsafe(32)),
        is_admin=True,
        created_at=datetime.utcnow(),
        last_login_at=None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


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
