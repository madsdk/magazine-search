from fastapi import FastAPI, Request
from jinja2 import pass_context
from starlette.middleware.sessions import SessionMiddleware

from magsearch.settings import get_settings
from magsearch.web.deps import csrf_token
from magsearch.web.middleware import AuthMiddleware
from magsearch.web.routes import router as content_router
from magsearch.web.routes_admin import router as admin_router
from magsearch.web.routes_auth import router as auth_router


def _wire_jinja_globals() -> None:
    """Expose csrf_token() and current_user as Jinja globals on every template env."""
    from magsearch.web import routes, routes_admin, routes_auth

    @pass_context
    def _csrf(ctx) -> str:
        return csrf_token(ctx["request"])

    @pass_context
    def _current_user(ctx):
        request: Request = ctx["request"]
        return getattr(request.state, "current_user", None)

    for module in (routes, routes_admin, routes_auth):
        env = module._TEMPLATES.env  # type: ignore[attr-defined]
        env.globals["csrf_token"] = _csrf
        env.globals["current_user"] = _current_user


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Magazine Search")

    secret = settings.session_secret or "dev-insecure-secret-do-not-use-in-production"

    # Middleware added LAST runs OUTERMOST. SessionMiddleware must wrap
    # AuthMiddleware so the gate can read scope["session"].
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie="magsearch_session",
        same_site="lax",
        https_only=False,
        max_age=60 * 60 * 24 * 14,
    )

    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(content_router)

    _wire_jinja_globals()
    return app


app = create_app()
