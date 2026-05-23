"""Research-mode routes: private per-user research topics that bookmark
specific pages across magazines. Registered only when auth_enabled.
"""

from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from magsearch.models import (
    Magazine,
    Page,
    ResearchTopic,
    ResearchTopicPage,
    User,
)
from magsearch.web.deps import get_db, require_csrf, require_researcher

router = APIRouter(prefix="/research", dependencies=[Depends(require_researcher)])
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _humanize_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    now = datetime.utcnow()
    delta = now - value
    if delta < timedelta(minutes=1):
        return "just now"
    if delta < timedelta(hours=1):
        n = int(delta.total_seconds() // 60)
        return f"{n} minute{'s' if n != 1 else ''} ago"
    if delta < timedelta(days=1):
        n = int(delta.total_seconds() // 3600)
        return f"{n} hour{'s' if n != 1 else ''} ago"
    if delta < timedelta(days=7):
        n = delta.days
        return f"{n} day{'s' if n != 1 else ''} ago"
    if delta < timedelta(days=14):
        return "last week"
    if delta < timedelta(days=60):
        n = delta.days // 7
        return f"{n} weeks ago"
    if delta < timedelta(days=365):
        n = delta.days // 30
        return f"{n} months ago" if n > 1 else "last month"
    return value.strftime("%Y-%m-%d")


_TEMPLATES.env.filters["humanize_dt"] = _humanize_dt


def _owned_topic(db: Session, topic_id: int, user: User) -> ResearchTopic:
    """Load a topic that belongs to `user`. 404 if missing or not theirs
    (don't leak existence across users)."""
    topic = db.get(ResearchTopic, topic_id)
    if topic is None or topic.user_id != user.id:
        raise HTTPException(status_code=404, detail="topic not found")
    return topic


def _topic_page_count(db: Session, topic_id: int) -> int:
    return db.scalar(
        select(func.count(ResearchTopicPage.id)).where(
            ResearchTopicPage.topic_id == topic_id
        )
    ) or 0


def _user_topics_with_counts(db: Session, user_id: int) -> list[dict]:
    rows = db.execute(
        select(
            ResearchTopic,
            func.count(ResearchTopicPage.id).label("page_count"),
        )
        .outerjoin(
            ResearchTopicPage, ResearchTopicPage.topic_id == ResearchTopic.id
        )
        .where(ResearchTopic.user_id == user_id)
        .group_by(ResearchTopic.id)
        .order_by(ResearchTopic.updated_at.desc())
    ).all()
    return [{"topic": t, "page_count": int(c)} for (t, c) in rows]


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def topics_index(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_researcher),
) -> HTMLResponse:
    items = _user_topics_with_counts(db, user.id)
    return _TEMPLATES.TemplateResponse(
        request, "research/index.html", {"items": items}
    )


@router.post("")
@router.post("/")
def topic_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    user: User = Depends(require_researcher),
):
    name_clean = name.strip()
    if not name_clean:
        return RedirectResponse(url="/research", status_code=303)
    now = datetime.utcnow()
    topic = ResearchTopic(
        user_id=user.id,
        name=name_clean,
        description=description.strip() or None,
        created_at=now,
        updated_at=now,
    )
    db.add(topic)
    db.commit()
    return RedirectResponse(url=f"/research/{topic.id}", status_code=303)


def _sorted_topic_pages(
    db: Session, topic_id: int
) -> list[tuple[Magazine, list[tuple[ResearchTopicPage, Page]]]]:
    """Return saved pages for a topic grouped by magazine.

    Each magazine is paired with its saved-page rows in page-number order.
    Magazines themselves are ordered by publication_date (NULLs last), then
    by id for stability.
    """
    rows = db.execute(
        select(ResearchTopicPage, Page, Magazine)
        .join(Page, Page.id == ResearchTopicPage.page_id)
        .join(Magazine, Magazine.id == Page.magazine_id)
        .where(ResearchTopicPage.topic_id == topic_id)
        .order_by(
            Magazine.publication_date.asc().nullslast(),
            Magazine.id.asc(),
            Page.page_number.asc(),
        )
    ).all()

    grouped: list[tuple[Magazine, list[tuple[ResearchTopicPage, Page]]]] = []
    for tp, page, mag in rows:
        if not grouped or grouped[-1][0].id != mag.id:
            grouped.append((mag, []))
        grouped[-1][1].append((tp, page))
    return grouped


@router.get("/{topic_id}", response_class=HTMLResponse)
def topic_detail(
    request: Request,
    topic_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_researcher),
) -> HTMLResponse:
    topic = _owned_topic(db, topic_id, user)
    grouped = _sorted_topic_pages(db, topic.id)
    total_pages = sum(len(pages) for _, pages in grouped)
    return _TEMPLATES.TemplateResponse(
        request,
        "research/detail.html",
        {"topic": topic, "grouped": grouped, "total_pages": total_pages},
    )


@router.post("/{topic_id}/edit")
def topic_edit(
    request: Request,
    topic_id: int,
    name: str = Form(...),
    description: str = Form(""),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    user: User = Depends(require_researcher),
):
    topic = _owned_topic(db, topic_id, user)
    name_clean = name.strip()
    if name_clean:
        topic.name = name_clean
    topic.description = description.strip() or None
    topic.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(url=f"/research/{topic.id}", status_code=303)


@router.post("/{topic_id}/delete")
def topic_delete(
    request: Request,
    topic_id: int,
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    user: User = Depends(require_researcher),
):
    topic = _owned_topic(db, topic_id, user)
    db.delete(topic)
    db.commit()
    return RedirectResponse(url="/research", status_code=303)


@router.post("/{topic_id}/pages/{page_id}/note")
def topic_page_note(
    request: Request,
    topic_id: int,
    page_id: int,
    note: str = Form(""),
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    user: User = Depends(require_researcher),
):
    topic = _owned_topic(db, topic_id, user)
    row = db.scalar(
        select(ResearchTopicPage).where(
            ResearchTopicPage.topic_id == topic.id,
            ResearchTopicPage.page_id == page_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="saved page not found")
    row.note = note.strip() or None
    topic.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(url=f"/research/{topic.id}", status_code=303)


@router.post("/{topic_id}/pages/{page_id}/remove")
def topic_page_remove(
    request: Request,
    topic_id: int,
    page_id: int,
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    user: User = Depends(require_researcher),
):
    topic = _owned_topic(db, topic_id, user)
    row = db.scalar(
        select(ResearchTopicPage).where(
            ResearchTopicPage.topic_id == topic.id,
            ResearchTopicPage.page_id == page_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="saved page not found")
    db.delete(row)
    topic.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(url=f"/research/{topic.id}", status_code=303)


@router.post("/save-page")
async def save_page(
    request: Request,
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    user: User = Depends(require_researcher),
):
    """Reconcile a page's topic memberships in a single submit.

    Form fields:
        page_id            (int, required)
        topic_ids          (repeated int, optional — the topics this page
                           should belong to *after* this submission)
        note               (str, optional — applied to every membership
                           that's created or already exists in this submit)
        new_topic_name     (str, optional — if non-empty, create a new
                           topic owned by current user and include it in
                           the membership set)
        return_to          (str, optional — URL to redirect to; defaults
                           to /research)
    """
    form = await request.form()

    try:
        page_id = int(form.get("page_id") or "")
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid page_id")

    page = db.get(Page, page_id)
    if page is None:
        raise HTTPException(status_code=404, detail="page not found")

    submitted_ids: set[int] = set()
    for raw in form.getlist("topic_ids"):
        try:
            tid = int(raw)
        except ValueError:
            continue
        submitted_ids.add(tid)

    # Verify every submitted topic belongs to this user — silently drop
    # foreign ones rather than 403, so a stale tab can't make the request
    # fail catastrophically.
    owned_topic_ids: set[int] = set()
    if submitted_ids:
        rows = db.scalars(
            select(ResearchTopic.id).where(
                ResearchTopic.id.in_(submitted_ids),
                ResearchTopic.user_id == user.id,
            )
        ).all()
        owned_topic_ids = set(int(r) for r in rows)

    new_topic_name = (form.get("new_topic_name") or "").strip()
    note = (form.get("note") or "").strip() or None
    now = datetime.utcnow()

    if new_topic_name:
        new_topic = ResearchTopic(
            user_id=user.id,
            name=new_topic_name,
            description=None,
            created_at=now,
            updated_at=now,
        )
        db.add(new_topic)
        db.flush()
        owned_topic_ids.add(new_topic.id)

    # Compute add/remove against the current membership for this page,
    # scoped to topics the current user actually owns.
    existing_rows = db.execute(
        select(ResearchTopicPage, ResearchTopic)
        .join(ResearchTopic, ResearchTopic.id == ResearchTopicPage.topic_id)
        .where(
            ResearchTopicPage.page_id == page_id,
            ResearchTopic.user_id == user.id,
        )
    ).all()
    existing_by_topic: dict[int, ResearchTopicPage] = {
        rt.id: rtp for (rtp, rt) in existing_rows
    }
    existing_ids = set(existing_by_topic.keys())

    to_add = owned_topic_ids - existing_ids
    to_remove = existing_ids - owned_topic_ids
    # Topics that were already members and remain so: refresh their note
    # only if the user typed a non-empty note this time (a blank note
    # preserves the old one rather than silently clearing it).
    to_update = owned_topic_ids & existing_ids

    touched_topic_ids: set[int] = set()

    for tid in to_add:
        db.add(
            ResearchTopicPage(
                topic_id=tid,
                page_id=page_id,
                note=note,
                saved_at=now,
            )
        )
        touched_topic_ids.add(tid)

    for tid in to_remove:
        db.delete(existing_by_topic[tid])
        touched_topic_ids.add(tid)

    if note is not None:
        for tid in to_update:
            existing_by_topic[tid].note = note
            touched_topic_ids.add(tid)

    if touched_topic_ids:
        db.execute(
            ResearchTopic.__table__.update()
            .where(ResearchTopic.id.in_(touched_topic_ids))
            .values(updated_at=now)
        )

    db.commit()

    return_to = form.get("return_to") or "/research"
    # Guard against open-redirect: only honor same-origin relative paths.
    # Reject scheme-relative URLs like "//evil.example" that browsers treat
    # as cross-origin even though they start with "/".
    if (
        not isinstance(return_to, str)
        or not return_to.startswith("/")
        or return_to.startswith("//")
    ):
        return_to = "/research"
    return RedirectResponse(url=return_to, status_code=303)


def existing_topic_ids_for_page(
    db: Session, page_id: int, user_id: int
) -> set[int]:
    """Topics owned by user_id that currently contain this page. Helper
    for the page-viewer template so it can pre-check chips."""
    rows = db.scalars(
        select(ResearchTopicPage.topic_id)
        .join(ResearchTopic, ResearchTopic.id == ResearchTopicPage.topic_id)
        .where(
            ResearchTopicPage.page_id == page_id,
            ResearchTopic.user_id == user_id,
        )
    ).all()
    return set(int(r) for r in rows)


def user_topics(db: Session, user_id: int) -> list[ResearchTopic]:
    """All topics owned by user_id, ordered by name (case-insensitive)."""
    return list(
        db.scalars(
            select(ResearchTopic)
            .where(ResearchTopic.user_id == user_id)
            .order_by(func.lower(ResearchTopic.name))
        ).all()
    )
