from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from jinja2 import pass_context
from starlette.middleware.sessions import SessionMiddleware

from magsearch.settings import get_settings
from magsearch.web.deps import csrf_token
from magsearch.web.middleware import AuthMiddleware, BodySizeLimitMiddleware
from magsearch.web.routes import router as content_router
from magsearch.web.routes_admin import router as admin_router
from magsearch.web.routes_auth import router as auth_router


def _wire_jinja_globals(auth_enabled: bool) -> None:
    """Expose csrf_token(), current_user, and auth_enabled as Jinja globals."""
    from magsearch.web import routes, routes_admin, routes_auth, routes_import

    @pass_context
    def _csrf(ctx) -> str:
        return csrf_token(ctx["request"])

    @pass_context
    def _current_user(ctx):
        request: Request = ctx["request"]
        return getattr(request.state, "current_user", None)

    for module in (routes, routes_admin, routes_auth, routes_import):
        env = module._TEMPLATES.env  # type: ignore[attr-defined]
        env.globals["csrf_token"] = _csrf
        env.globals["current_user"] = _current_user
        env.globals["auth_enabled"] = auth_enabled


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Magazine Search")

    secret = settings.session_secret or "dev-insecure-secret-do-not-use-in-production"

    # Middleware added LAST runs OUTERMOST. SessionMiddleware must wrap
    # AuthMiddleware so the gate can read scope["session"]. BodySizeLimit
    # must be OUTERMOST so it terminates oversized request bodies before
    # any other layer (including FastAPI's multipart parser) reads them.
    # Its scoped path depends on which upload endpoint is registered.
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="magsearch_session",
        same_site="lax",
        https_only=False,
        max_age=60 * 60 * 24 * 14,
    )
    upload_path = "/import" if not settings.auth_enabled else "/admin/issues/upload"
    app.add_middleware(BodySizeLimitMiddleware, path=upload_path)

    app.include_router(auth_router)
    app.include_router(admin_router)
    # The public multi-file import route only exists in desktop (auth-off)
    # mode. Web admin deployments continue to use /admin/issues/upload.
    if not settings.auth_enabled:
        from magsearch.web.routes_import import router as import_router
        app.include_router(import_router)
    app.include_router(content_router)

    # Vendored Tailwind CSS + Google Fonts so the desktop bundle runs offline.
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    _wire_jinja_globals(settings.auth_enabled)
    return app


app = create_app()
