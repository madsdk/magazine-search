from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from magsearch.models import User
from magsearch.web.auth import normalize_username, verify_password
from magsearch.web.deps import get_db, require_csrf

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _safe_next(raw: str | None) -> str:
    """Allow only same-origin relative paths to prevent open-redirect."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    return raw


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "") -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "login.html", {"next": _safe_next(next), "error": None}
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    normalized = normalize_username(username)
    target = _safe_next(next)
    user = db.scalar(select(User).where(User.username == normalized))
    if user is None or not verify_password(password, user.password_hash):
        return _TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"next": target, "error": "Invalid username or password."},
            status_code=401,
        )
    request.session["user_id"] = user.id
    user.last_login_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(url=target, status_code=303)


@router.post("/logout")
def logout(
    request: Request,
    _csrf: None = Depends(require_csrf),
):
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
