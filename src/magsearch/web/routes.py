from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from magsearch.models import Magazine
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
    results = search(db, q, offset=offset, limit=_PAGE_SIZE)
    return _TEMPLATES.TemplateResponse(
        request,
        "search.html",
        {"q": q, "results": results, "page": page,
         "has_more": len(results) == _PAGE_SIZE},
    )
