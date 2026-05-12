from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from magsearch.models import Magazine
from magsearch.models import Page as PageModel
from magsearch.settings import Settings, get_settings
from magsearch.web.deps import get_db
from magsearch.web.search import search

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_PAGE_SIZE = 25


@router.get("/", response_class=HTMLResponse)
def landing(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    recent = db.scalars(
        select(Magazine).order_by(Magazine.ingested_at.desc()).limit(6)
    ).all()
    return _TEMPLATES.TemplateResponse(request, "index.html", {"recent": recent})


@router.get("/search", response_class=HTMLResponse)
def search_route(
    request: Request,
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    offset = (page - 1) * _PAGE_SIZE
    results = search(db, q, offset=offset, limit=_PAGE_SIZE + 1)
    has_more = len(results) > _PAGE_SIZE
    results = results[:_PAGE_SIZE]
    return _TEMPLATES.TemplateResponse(
        request,
        "search.html",
        {"q": q, "results": results, "page": page, "has_more": has_more},
    )


@router.get("/magazines", response_class=HTMLResponse)
def magazines_index(
    request: Request,
    publisher: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    stmt = select(Magazine).order_by(Magazine.publication_date.desc().nullslast())
    if publisher:
        stmt = stmt.where(Magazine.publisher == publisher)
    magazines = db.scalars(stmt).all()
    all_publishers = db.scalars(
        select(Magazine.publisher).where(Magazine.publisher.is_not(None)).distinct()
    ).all()
    return _TEMPLATES.TemplateResponse(
        request,
        "magazines.html",
        {"magazines": magazines, "publishers": all_publishers, "active": publisher},
    )


@router.get("/magazine/{magazine_id}", response_class=HTMLResponse)
def magazine_detail(
    request: Request,
    magazine_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    mag = db.get(Magazine, magazine_id)
    if mag is None:
        raise HTTPException(status_code=404, detail="magazine not found")
    return _TEMPLATES.TemplateResponse(
        request,
        "magazine.html",
        {"mag": mag, "pages": sorted(mag.pages, key=lambda p: p.page_number)},
    )


@router.get("/magazine/{magazine_id}/page/{page_number}", response_class=HTMLResponse)
def page_view(
    request: Request,
    magazine_id: str,
    page_number: int,
    q: str = Query(default=""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    mag = db.get(Magazine, magazine_id)
    if mag is None:
        raise HTTPException(status_code=404, detail="magazine not found")
    page = db.scalar(
        select(PageModel).where(
            PageModel.magazine_id == magazine_id,
            PageModel.page_number == page_number,
        )
    )
    if page is None:
        raise HTTPException(status_code=404, detail="page not found")
    return _TEMPLATES.TemplateResponse(
        request,
        "page.html",
        {
            "mag": mag,
            "page": page,
            "q": q,
            "has_prev": page_number > 1,
            "has_next": page_number < mag.page_count,
        },
    )


@router.get("/bundle/{path:path}")
def serve_bundle(path: str, settings: Settings = Depends(get_settings)):
    root = settings.bundles_dir.resolve()
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(candidate)
