from itertools import groupby
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from magsearch.models import Magazine
from magsearch.models import Page as PageModel
from magsearch.settings import Settings, get_settings
from magsearch.web.deps import get_db
from magsearch.web.search import (
    DEFAULT_FLAT_SORT,
    DEFAULT_PER_ISSUE_SORT,
    FLAT_SORT_OPTIONS,
    PER_ISSUE_SORT_OPTIONS,
    search,
    search_in_magazine,
    search_in_magazine_title,
    search_magazines,
)

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

# Page-size whitelist. Bare 25 is the historical default.
PER_PAGE_OPTIONS = (25, 50, 100)
DEFAULT_PER_PAGE = 25

# Hard total-result caps. Beyond these, pagination ends and the UI shows
# "SHOWING TOP N MATCHES — REFINE YOUR QUERY". Bounds OFFSET-on-rank cost
# at production scale (100k+ pages).
FLAT_RESULT_CAP = 1000      # /search?view=flat and /magazines/{title}?q=…
GROUPED_RESULT_CAP = 200    # /search?view=grouped (caps magazines, not pages)


def _validate_per_page(value: int) -> int:
    return value if value in PER_PAGE_OPTIONS else DEFAULT_PER_PAGE


def _validate_sort(value: str, options: tuple[str, ...], default: str) -> str:
    return value if value in options else default


def _max_page(result_cap: int, per_page: int) -> int:
    # ceil(cap / per_page), guaranteed >= 1
    return max(1, -(-result_cap // per_page))


def _clamp_page(page: int, result_cap: int, per_page: int) -> tuple[int, int]:
    """Returns (clamped_page, max_page)."""
    max_page = _max_page(result_cap, per_page)
    return min(max(page, 1), max_page), max_page


@router.get("/", response_class=HTMLResponse)
def landing(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    recent = db.scalars(
        select(Magazine).order_by(Magazine.ingested_at.desc()).limit(6)
    ).all()
    total_issues = db.scalar(select(func.count(Magazine.id))) or 0
    total_pages = db.scalar(
        select(func.coalesce(func.sum(Magazine.page_count), 0))
    ) or 0
    return _TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "recent": recent,
            "total_issues": total_issues,
            "total_pages": total_pages,
        },
    )


@router.get("/search", response_class=HTMLResponse)
def search_route(
    request: Request,
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    view: str = Query(default="grouped"),
    sort: str = Query(default=DEFAULT_FLAT_SORT),
    per_page: int = Query(default=DEFAULT_PER_PAGE),
    match_all: int = Query(default=1),
    match_phrase: int = Query(default=0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if view not in ("grouped", "flat"):
        view = "grouped"
    sort = _validate_sort(sort, FLAT_SORT_OPTIONS, DEFAULT_FLAT_SORT)
    per_page = _validate_per_page(per_page)
    match_phrase_b = bool(match_phrase)
    # Phrase mode implies AND: you can't find every word in order unless
    # every word is on the page. Lock match_all on whenever phrase is on
    # so the rendered checkbox state never contradicts the search behavior.
    match_all_b = bool(match_all) or match_phrase_b
    result_cap = FLAT_RESULT_CAP if view == "flat" else GROUPED_RESULT_CAP
    page, max_page = _clamp_page(page, result_cap, per_page)

    offset = (page - 1) * per_page
    if view == "flat":
        results = search(
            db, q, offset=offset, limit=per_page + 1, sort=sort,
            match_all=match_all_b, match_phrase=match_phrase_b,
        )
    else:
        results = search_magazines(
            db, q, offset=offset, limit=per_page + 1, sort=sort,
            match_all=match_all_b, match_phrase=match_phrase_b,
        )
    has_more = len(results) > per_page and page < max_page
    results = results[:per_page]
    at_result_cap = page >= max_page and len(results) >= per_page
    return _TEMPLATES.TemplateResponse(
        request,
        "search.html",
        {
            "q": q,
            "results": results,
            "page": page,
            "has_more": has_more,
            "view": view,
            "sort": sort,
            "per_page": per_page,
            "per_page_options": PER_PAGE_OPTIONS,
            "sort_options": FLAT_SORT_OPTIONS,
            "at_result_cap": at_result_cap,
            "result_cap": result_cap,
            "match_all": match_all_b,
            "match_phrase": match_phrase_b,
        },
    )


@router.get("/magazines", response_class=HTMLResponse)
def magazines_index(
    request: Request,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    stmt = select(Magazine).order_by(
        Magazine.title, Magazine.publication_date.asc().nullslast()
    )
    rows = db.scalars(stmt).all()
    titles = []
    for title, group in groupby(rows, key=lambda m: m.title):
        items = list(group)
        earliest = items[0]  # ordered ascending, so first = earliest
        titles.append({
            "title": title,
            "issue_count": len(items),
            "cover_path": earliest.cover_path,
            "publisher": earliest.publisher,
        })
    return _TEMPLATES.TemplateResponse(
        request,
        "magazines.html",
        {"titles": titles},
    )


@router.get("/magazines/{title}", response_class=HTMLResponse)
def magazine_issues(
    request: Request,
    title: str,
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    sort: str = Query(default=DEFAULT_FLAT_SORT),
    per_page: int = Query(default=DEFAULT_PER_PAGE),
    match_all: int = Query(default=1),
    match_phrase: int = Query(default=0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    issues = db.scalars(
        select(Magazine)
        .where(Magazine.title == title)
        .order_by(Magazine.publication_date.asc().nullslast())
    ).all()
    if not issues:
        raise HTTPException(status_code=404, detail="magazine not found")

    sort = _validate_sort(sort, FLAT_SORT_OPTIONS, DEFAULT_FLAT_SORT)
    per_page = _validate_per_page(per_page)
    match_phrase_b = bool(match_phrase)
    match_all_b = bool(match_all) or match_phrase_b

    results: list | None = None
    has_more = False
    at_result_cap = False
    max_page = 1
    if q.strip():
        page, max_page = _clamp_page(page, FLAT_RESULT_CAP, per_page)
        offset = (page - 1) * per_page
        hits = search_in_magazine_title(
            db, q, title, offset=offset, limit=per_page + 1, sort=sort,
            match_all=match_all_b, match_phrase=match_phrase_b,
        )
        has_more = len(hits) > per_page and page < max_page
        results = hits[:per_page]
        at_result_cap = page >= max_page and len(results) >= per_page
    return _TEMPLATES.TemplateResponse(
        request,
        "magazine_issues.html",
        {
            "title": title,
            "issues": issues,
            "q": q,
            "results": results,
            "page": page,
            "has_more": has_more,
            "sort": sort,
            "per_page": per_page,
            "per_page_options": PER_PAGE_OPTIONS,
            "sort_options": FLAT_SORT_OPTIONS,
            "at_result_cap": at_result_cap,
            "result_cap": FLAT_RESULT_CAP,
            "match_all": match_all_b,
            "match_phrase": match_phrase_b,
        },
    )


@router.get("/magazine/{magazine_id}", response_class=HTMLResponse)
def magazine_detail(
    request: Request,
    magazine_id: str,
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    sort: str = Query(default=DEFAULT_PER_ISSUE_SORT),
    per_page: int = Query(default=DEFAULT_PER_PAGE),
    match_all: int = Query(default=1),
    match_phrase: int = Query(default=0),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    mag = db.get(Magazine, magazine_id)
    if mag is None:
        raise HTTPException(status_code=404, detail="magazine not found")

    sort = _validate_sort(sort, PER_ISSUE_SORT_OPTIONS, DEFAULT_PER_ISSUE_SORT)
    per_page = _validate_per_page(per_page)
    match_phrase_b = bool(match_phrase)
    match_all_b = bool(match_all) or match_phrase_b

    results: list | None = None
    has_more = False
    if q.strip():
        page = max(page, 1)
        offset = (page - 1) * per_page
        hits = search_in_magazine(
            db, q, magazine_id, offset=offset, limit=per_page + 1, sort=sort,
            match_all=match_all_b, match_phrase=match_phrase_b,
        )
        has_more = len(hits) > per_page
        results = hits[:per_page]
    return _TEMPLATES.TemplateResponse(
        request,
        "magazine.html",
        {
            "mag": mag,
            "pages": sorted(mag.pages, key=lambda p: p.page_number),
            "q": q,
            "results": results,
            "page": page,
            "has_more": has_more,
            "sort": sort,
            "per_page": per_page,
            "per_page_options": PER_PAGE_OPTIONS,
            "sort_options": PER_ISSUE_SORT_OPTIONS,
            "match_all": match_all_b,
            "match_phrase": match_phrase_b,
        },
    )


@router.get("/magazine/{magazine_id}/page/{page_number}", response_class=HTMLResponse)
def page_view(
    request: Request,
    magazine_id: str,
    page_number: int,
    q: str = Query(default=""),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
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

    # Research-mode data: only loaded for users who actually see the
    # save-to-research UI (must be logged in AND have the researcher
    # role). Desktop mode (auth_enabled=False) and non-researcher users
    # skip this entirely — saves two queries per page view and keeps
    # other users' topic names out of the template context.
    research_topics: list = []
    research_topic_ids: set[int] = set()
    current_user = getattr(request.state, "current_user", None)
    if (
        settings.auth_enabled
        and current_user is not None
        and current_user.is_researcher
    ):
        from magsearch.web.routes_research import (
            existing_topic_ids_for_page,
            user_topics,
        )
        research_topics = user_topics(db, current_user.id)
        research_topic_ids = existing_topic_ids_for_page(
            db, page.id, current_user.id
        )

    return _TEMPLATES.TemplateResponse(
        request,
        "page.html",
        {
            "mag": mag,
            "page": page,
            "q": q,
            "has_prev": page_number > 1,
            "has_next": page_number < mag.page_count,
            "research_topics": research_topics,
            "research_topic_ids": research_topic_ids,
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
