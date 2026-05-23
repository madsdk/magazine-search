import shutil
import tempfile
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from magsearch.importer import delete_bundle_dir, import_bundle
from magsearch.importer import ImportError as MagImportError
from magsearch.models import Magazine, User
from magsearch.settings import Settings, get_settings
from magsearch.web.auth import hash_password, normalize_username
from magsearch.web.bundle_upload import BundleUploadError, extract_and_stage
from magsearch.web.config_service import CONFIG_REGISTRY, get_all, set_value
from magsearch.web.deps import get_db, require_admin, require_csrf

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _parse_date(raw: str | None) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"invalid date: {raw!r}")


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    mag_count = db.scalar(select(func.count(Magazine.id))) or 0
    user_count = db.scalar(select(func.count(User.id))) or 0
    return _TEMPLATES.TemplateResponse(
        request,
        "admin/dashboard.html",
        {
            "mag_count": mag_count,
            "user_count": user_count,
            "config": get_all(db),
        },
    )


# ------- Issues -------

@router.get("/issues", response_class=HTMLResponse)
def issues_index(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
) -> HTMLResponse:
    stmt = select(Magazine).order_by(Magazine.title, Magazine.publication_date.asc().nullslast())
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(Magazine.title.ilike(like) | Magazine.issue.ilike(like))
    issues = db.scalars(stmt).all()
    return _TEMPLATES.TemplateResponse(
        request, "admin/issues.html", {"issues": issues, "q": q}
    )


@router.get("/issues/upload", response_class=HTMLResponse)
def issue_upload_form(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "admin/issue_upload.html", {"error": None}
    )


@router.post("/issues/upload")
def issue_upload_submit(
    request: Request,
    bundle: UploadFile,
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    # Stream the upload to a real temp file with a hard byte cap so an
    # oversized body can't fill the disk before extract_and_stage runs.
    tmp_dir = Path(tempfile.mkdtemp(prefix="magsearch-upload-"))
    tmp_zip = tmp_dir / "upload.zip"
    try:
        cap = settings.max_upload_bytes
        bytes_written = 0
        with tmp_zip.open("wb") as out:
            while True:
                chunk = bundle.file.read(64 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > cap:
                    return _TEMPLATES.TemplateResponse(
                        request,
                        "admin/issue_upload.html",
                        {"error": f"upload body exceeds max size of {cap // (1024 * 1024)} MB"},
                        status_code=413,
                    )
                out.write(chunk)

        try:
            staged = extract_and_stage(
                tmp_zip,
                settings.bundles_dir,
                max_uncompressed_bytes=settings.max_upload_bytes,
            )
        except BundleUploadError as exc:
            return _TEMPLATES.TemplateResponse(
                request,
                "admin/issue_upload.html",
                {"error": str(exc)},
                status_code=400,
            )

        try:
            magazine_id = import_bundle(staged, db)
            db.commit()
        except MagImportError as exc:
            return _TEMPLATES.TemplateResponse(
                request,
                "admin/issue_upload.html",
                {"error": f"Import failed: {exc}"},
                status_code=400,
            )

        return RedirectResponse(
            url=f"/admin/issues/{magazine_id}/edit", status_code=303,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.get("/issues/{magazine_id}/edit", response_class=HTMLResponse)
def issue_edit_form(
    request: Request, magazine_id: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    mag = db.get(Magazine, magazine_id)
    if mag is None:
        raise HTTPException(status_code=404, detail="magazine not found")
    return _TEMPLATES.TemplateResponse(
        request, "admin/issue_edit.html", {"mag": mag, "error": None}
    )


@router.post("/issues/{magazine_id}/edit")
def issue_edit_submit(
    request: Request,
    magazine_id: str,
    title: str = Form(...),
    issue: str = Form(""),
    publication_date: str = Form(""),
    publisher: str = Form(""),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    mag = db.get(Magazine, magazine_id)
    if mag is None:
        raise HTTPException(status_code=404, detail="magazine not found")
    mag.title = title.strip() or mag.title
    mag.issue = issue.strip() or None
    mag.publication_date = _parse_date(publication_date)
    mag.publisher = publisher.strip() or None
    db.commit()
    return RedirectResponse(url="/admin/issues", status_code=303)


@router.get("/magazines/{title}/missing-issues", response_class=HTMLResponse)
def magazine_missing_issues(
    request: Request, title: str, db: Session = Depends(get_db)
) -> HTMLResponse:
    issues = db.scalars(
        select(Magazine)
        .where(Magazine.title == title)
        .order_by(Magazine.publication_date.asc().nullslast())
    ).all()
    if not issues:
        raise HTTPException(status_code=404, detail="magazine not found")

    present: set[int] = set()
    excluded: list[Magazine] = []
    for m in issues:
        raw = (m.issue or "").strip()
        try:
            n = int(raw)
        except ValueError:
            excluded.append(m)
            continue
        if n < 1:
            excluded.append(m)
            continue
        present.add(n)

    max_issue = max(present) if present else None
    missing = sorted(set(range(1, max_issue + 1)) - present) if max_issue else []

    return _TEMPLATES.TemplateResponse(
        request,
        "admin/missing_issues.html",
        {
            "title": title,
            "missing": missing,
            "max_issue": max_issue,
            "present_count": len(present),
            "excluded": excluded,
        },
    )


@router.post("/issues/{magazine_id}/delete")
def issue_delete(
    request: Request,
    magazine_id: str,
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    mag = db.get(Magazine, magazine_id)
    if mag is None:
        raise HTTPException(status_code=404, detail="magazine not found")
    db.delete(mag)
    db.commit()
    # DB-first, then disk: a stray bundle dir is recoverable; a dangling DB row isn't.
    delete_bundle_dir(settings.bundles_dir, magazine_id)
    return RedirectResponse(url="/admin/issues", status_code=303)


# ------- Users -------

@router.get("/users", response_class=HTMLResponse)
def users_index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    users = db.scalars(select(User).order_by(User.username)).all()
    return _TEMPLATES.TemplateResponse(request, "admin/users.html", {"users": users})


@router.get("/users/new", response_class=HTMLResponse)
def user_new_form(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "admin/user_form.html", {"user": None, "error": None}
    )


@router.post("/users/new")
def user_new_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    is_admin: str = Form(""),
    is_researcher: str = Form(""),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    normalized = normalize_username(username)
    if not normalized:
        return _TEMPLATES.TemplateResponse(
            request, "admin/user_form.html",
            {"user": None, "error": "Username must not be empty."},
            status_code=400,
        )
    if not password:
        return _TEMPLATES.TemplateResponse(
            request, "admin/user_form.html",
            {"user": None, "error": "Password must not be empty."},
            status_code=400,
        )
    existing = db.scalar(select(User).where(User.username == normalized))
    if existing is not None:
        return _TEMPLATES.TemplateResponse(
            request, "admin/user_form.html",
            {"user": None, "error": f"User '{normalized}' already exists."},
            status_code=400,
        )
    db.add(User(
        username=normalized,
        password_hash=hash_password(password),
        is_admin=bool(is_admin),
        is_researcher=bool(is_researcher),
        created_at=datetime.utcnow(),
        last_login_at=None,
    ))
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
def user_edit_form(
    request: Request, user_id: int, db: Session = Depends(get_db)
) -> HTMLResponse:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return _TEMPLATES.TemplateResponse(
        request, "admin/user_form.html", {"user": user, "error": None}
    )


def _admin_count(db: Session) -> int:
    return db.scalar(select(func.count(User.id)).where(User.is_admin.is_(True))) or 0


@router.post("/users/{user_id}/edit")
def user_edit_submit(
    request: Request,
    user_id: int,
    username: str = Form(...),
    password: str = Form(""),
    is_admin: str = Form(""),
    is_researcher: str = Form(""),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")

    new_username = normalize_username(username)
    if new_username and new_username != user.username:
        clash = db.scalar(select(User).where(User.username == new_username))
        if clash is not None and clash.id != user.id:
            return _TEMPLATES.TemplateResponse(
                request, "admin/user_form.html",
                {"user": user, "error": f"Username '{new_username}' is taken."},
                status_code=400,
            )
        user.username = new_username

    new_is_admin = bool(is_admin)
    if user.is_admin and not new_is_admin and _admin_count(db) <= 1:
        return _TEMPLATES.TemplateResponse(
            request, "admin/user_form.html",
            {"user": user, "error": "Cannot demote the last admin."},
            status_code=400,
        )
    user.is_admin = new_is_admin
    user.is_researcher = bool(is_researcher)

    if password.strip():
        user.password_hash = hash_password(password)

    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/delete")
def user_delete(
    request: Request,
    user_id: int,
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    if user.id == current.id:
        raise HTTPException(status_code=400, detail="cannot delete yourself")
    if user.is_admin and _admin_count(db) <= 1:
        raise HTTPException(status_code=400, detail="cannot delete the last admin")
    db.delete(user)
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=303)


# ------- Config -------

@router.get("/config", response_class=HTMLResponse)
def config_index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "admin/config.html",
        {"registry": CONFIG_REGISTRY, "values": get_all(db)},
    )


@router.post("/config")
async def config_submit(
    request: Request,
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    form = await request.form()
    for key, meta in CONFIG_REGISTRY.items():
        if meta["type"] == "bool":
            set_value(db, key, key in form)
        else:
            raw = form.get(key)
            if raw is None:
                continue
            set_value(db, key, raw)
    db.commit()
    return RedirectResponse(url="/admin/config", status_code=303)
