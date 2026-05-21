import secrets
from collections.abc import Iterator
from functools import lru_cache
from urllib.parse import quote

from fastapi import Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from magsearch.db import make_engine, make_session_factory
from magsearch.models import User
from magsearch.settings import Settings, get_settings


@lru_cache(maxsize=1)
def _session_factory_for(database_url: str) -> sessionmaker[Session]:
    return make_session_factory(make_engine(database_url))


def get_db(settings: Settings = Depends(get_settings)) -> Iterator[Session]:
    factory = _session_factory_for(settings.database_url)
    session = factory()
    try:
        yield session
    finally:
        session.close()


def get_current_user(
    request: Request, db: Session = Depends(get_db)
) -> User | None:
    # AuthMiddleware pins the resolved user onto request.state (either the
    # session-authenticated user, or the synthesized 'local' admin in
    # desktop mode). Prefer it over re-querying so desktop mode works even
    # without a session.
    pinned = getattr(request.state, "current_user", None)
    if pinned is not None:
        return pinned
    user_id = request.session.get("user_id") if hasattr(request, "session") else None
    if not user_id:
        return None
    user = db.get(User, user_id)
    return user


def require_user(
    request: Request, user: User | None = Depends(get_current_user)
) -> User:
    if user is None:
        next_url = quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""))
        raise HTTPException(
            status_code=303,
            headers={"Location": f"/login?next={next_url}"},
        )
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin access required")
    return user


def _ensure_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def require_csrf(
    request: Request, csrf_token: str = Form(...)
) -> None:
    expected = request.session.get("csrf_token") if hasattr(request, "session") else None
    if not expected or not secrets.compare_digest(expected, csrf_token):
        raise HTTPException(status_code=403, detail="invalid CSRF token")


def csrf_token(request: Request) -> str:
    """Jinja helper: read-or-create the per-session CSRF token."""
    return _ensure_csrf_token(request)
