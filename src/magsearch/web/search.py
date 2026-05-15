import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


_SAFE_WORD = re.compile(r"[A-Za-z0-9]+")


def sanitize_query(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    # Balance double quotes: if odd count, drop them all.
    if raw.count('"') % 2 != 0:
        raw = raw.replace('"', "")

    out_parts: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == '"':
            close = raw.find('"', i + 1)
            if close == -1:
                break
            inner = " ".join(_SAFE_WORD.findall(raw[i + 1:close]))
            if inner:
                out_parts.append(f'"{inner}"')
            i = close + 1
        else:
            m = _SAFE_WORD.match(raw, i)
            if m:
                out_parts.append(m.group(0))
                i = m.end()
            else:
                i += 1
    return " ".join(out_parts)


@dataclass(frozen=True)
class SearchResult:
    magazine_id: str
    magazine_title: str
    magazine_issue: str | None
    magazine_date: date | None
    page_number: int
    thumb_path: str
    snippet: str


@dataclass(frozen=True)
class MagazineMatch:
    magazine_id: str
    magazine_title: str
    magazine_issue: str | None
    magazine_date: date | None
    cover_path: str
    match_count: int


_SEARCH_SQL = text("""
    SELECT
        magazines.id               AS magazine_id,
        magazines.title            AS magazine_title,
        magazines.issue            AS magazine_issue,
        magazines.publication_date AS magazine_date,
        pages.page_number          AS page_number,
        pages.thumb_path           AS thumb_path,
        snippet(pages_fts, 0, '<mark>', '</mark>', '…', 16) AS snippet
    FROM pages_fts
    JOIN pages     ON pages_fts.rowid = pages.id
    JOIN magazines ON pages.magazine_id = magazines.id
    WHERE pages_fts MATCH :q
    ORDER BY rank, magazines.title, magazines.publication_date, pages.page_number
    LIMIT :limit OFFSET :offset
""").bindparams(bindparam("q"), bindparam("limit"), bindparam("offset"))


_SEARCH_IN_MAGAZINE_SQL = text("""
    SELECT
        magazines.id               AS magazine_id,
        magazines.title            AS magazine_title,
        magazines.issue            AS magazine_issue,
        magazines.publication_date AS magazine_date,
        pages.page_number          AS page_number,
        pages.thumb_path           AS thumb_path,
        snippet(pages_fts, 0, '<mark>', '</mark>', '…', 16) AS snippet
    FROM pages_fts
    JOIN pages     ON pages_fts.rowid = pages.id
    JOIN magazines ON pages.magazine_id = magazines.id
    WHERE pages_fts MATCH :q AND pages.magazine_id = :magazine_id
    ORDER BY rank, pages.page_number
    LIMIT :limit OFFSET :offset
""").bindparams(
    bindparam("q"), bindparam("magazine_id"), bindparam("limit"), bindparam("offset")
)


_SEARCH_MAGAZINES_SQL = text("""
    SELECT
        magazines.id               AS magazine_id,
        magazines.title            AS magazine_title,
        magazines.issue            AS magazine_issue,
        magazines.publication_date AS magazine_date,
        magazines.cover_path       AS cover_path,
        COUNT(*)                   AS match_count,
        MIN(pages_fts.rank)        AS best_rank
    FROM pages_fts
    JOIN pages     ON pages_fts.rowid = pages.id
    JOIN magazines ON pages.magazine_id = magazines.id
    WHERE pages_fts MATCH :q
    GROUP BY magazines.id
    ORDER BY best_rank, magazines.title, magazines.publication_date
    LIMIT :limit OFFSET :offset
""").bindparams(bindparam("q"), bindparam("limit"), bindparam("offset"))


def _coerce_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _row_to_result(r) -> SearchResult:
    return SearchResult(
        magazine_id=r.magazine_id,
        magazine_title=r.magazine_title,
        magazine_issue=r.magazine_issue,
        magazine_date=_coerce_date(r.magazine_date),
        page_number=r.page_number,
        thumb_path=r.thumb_path,
        snippet=r.snippet,
    )


def search(session: Session, raw_query: str, *, offset: int, limit: int) -> list[SearchResult]:
    q = sanitize_query(raw_query)
    if not q:
        return []
    try:
        rows = session.execute(_SEARCH_SQL, {"q": q, "limit": limit, "offset": offset}).all()
    except Exception:
        return []
    return [_row_to_result(r) for r in rows]


def search_in_magazine(
    session: Session,
    raw_query: str,
    magazine_id: str,
    *,
    offset: int,
    limit: int,
) -> list[SearchResult]:
    q = sanitize_query(raw_query)
    if not q:
        return []
    try:
        rows = session.execute(
            _SEARCH_IN_MAGAZINE_SQL,
            {"q": q, "magazine_id": magazine_id, "limit": limit, "offset": offset},
        ).all()
    except Exception:
        return []
    return [_row_to_result(r) for r in rows]


def search_magazines(
    session: Session, raw_query: str, *, offset: int, limit: int
) -> list[MagazineMatch]:
    q = sanitize_query(raw_query)
    if not q:
        return []
    try:
        rows = session.execute(
            _SEARCH_MAGAZINES_SQL, {"q": q, "limit": limit, "offset": offset}
        ).all()
    except Exception:
        return []
    return [
        MagazineMatch(
            magazine_id=r.magazine_id,
            magazine_title=r.magazine_title,
            magazine_issue=r.magazine_issue,
            magazine_date=_coerce_date(r.magazine_date),
            cover_path=r.cover_path,
            match_count=r.match_count,
        )
        for r in rows
    ]
