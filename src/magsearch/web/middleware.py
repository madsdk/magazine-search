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

        # Do all DB work in a tight scope so the pooled connection is released
        # BEFORE we await the downstream app. Holding a session across
        # `await self.app(...)` pins one connection per in-flight request and
        # exhausts the pool under concurrent / slow responses.
        with factory() as db:
            user = db.get(User, user_id) if user_id else None
            if user is not None:
                db.expunge(user)
            if allowed:
                require_login = False
            else:
                try:
                    require_login = get_value(db, "require_login")
                except Exception:
                    require_login = False

        if user is not None:
            request.state.current_user = user

        if allowed or not require_login or user is not None:
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


class BodySizeLimitMiddleware:
    """Reject POST bodies larger than `max_upload_bytes` *before* Starlette's
    multipart parser has a chance to spool them.

    A route handler that takes `bundle: UploadFile` runs after FastAPI has
    already parsed the multipart body — by then a malicious client has
    already filled the SpooledTemporaryFile (memory then disk) with gigabytes
    of data. Enforcing the cap here, at the ASGI layer, lets us terminate the
    request mid-stream by signalling `http.disconnect` to the app once we've
    seen too many body bytes go by.

    Scoped to one path so we don't add per-request overhead to every endpoint.
    """

    def __init__(self, app: ASGIApp, *, path: str = "/admin/issues/upload") -> None:
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != self.path
        ):
            await self.app(scope, receive, send)
            return

        max_bytes = get_settings().max_upload_bytes

        # Fast path: a declared Content-Length already over the cap — reject
        # without reading a single body byte.
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
                if declared is not None and declared > max_bytes:
                    await self._send_413(send, max_bytes)
                    return
                break

        received = 0
        rejected = False

        async def wrapped_receive():
            nonlocal received, rejected
            if rejected:
                # Once we've signalled rejection, tell the app the client
                # went away. Any in-progress body read stops cleanly.
                return {"type": "http.disconnect"}
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > max_bytes:
                    rejected = True
                    return {"type": "http.disconnect"}
            return message

        response_started = False

        async def wrapped_send(message):
            nonlocal response_started
            if rejected:
                # Swallow whatever the app emits while we're rejecting; we
                # send the 413 ourselves once the app returns or raises.
                return
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, wrapped_receive, wrapped_send)
        except Exception:
            # If the app raised because we cut its body off, that's expected;
            # fall through and send the 413. Any unrelated error propagates.
            if not rejected:
                raise

        if rejected and not response_started:
            await self._send_413(send, max_bytes)

    @staticmethod
    async def _send_413(send: Send, max_bytes: int) -> None:
        body = (
            f"upload body exceeds maximum size of {max_bytes // (1024 * 1024)} MB"
        ).encode()
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
