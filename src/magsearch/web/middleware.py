"""ASGI middleware that resolves the current user and (optionally) gates anonymous access.

Must be installed INSIDE SessionMiddleware so `scope["session"]` is populated.
"""
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from magsearch.models import User
from magsearch.settings import get_settings
from magsearch.web.config_service import get_value
from magsearch.web.deps import _session_factory_for


class AuthMiddleware:
    """Resolves the current user and enforces the require_login config flag.

    - Stashes the User object (if any) on `request.state.current_user` so templates
      can render a header indicator without re-querying.
    - When `require_login` is true, redirects anonymous requests to /login, except
      for the allow-list (/login, /logout, /static, /favicon).
    """

    ALLOW_PREFIXES = ("/login", "/logout", "/static", "/favicon")

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        request.state.current_user = None

        session = scope.get("session") or {}
        user_id = session.get("user_id") if isinstance(session, dict) else None

        settings = get_settings()
        factory = _session_factory_for(settings.database_url)
        path = scope.get("path", "")
        allowed = self._is_allowed(path)

        with factory() as db:
            user = db.get(User, user_id) if user_id else None
            if user is not None:
                db.expunge(user)
                request.state.current_user = user

            if allowed:
                await self.app(scope, receive, send)
                return

            try:
                require_login = get_value(db, "require_login")
            except Exception:
                require_login = False

            if not require_login or user is not None:
                await self.app(scope, receive, send)
                return

        target = path
        if scope.get("query_string"):
            target = f"{path}?{scope['query_string'].decode('latin-1')}"
        response = RedirectResponse(
            f"/login?next={quote(target, safe='')}", status_code=303
        )
        await response(scope, receive, send)

    @classmethod
    def _is_allowed(cls, path: str) -> bool:
        for prefix in cls.ALLOW_PREFIXES:
            if path == prefix or path.startswith(prefix + "/"):
                return True
        return False


# Back-compat alias for the name used in earlier drafts of the plan.
RequireLoginMiddleware = AuthMiddleware
