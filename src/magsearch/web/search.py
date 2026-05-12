import re
from dataclasses import dataclass

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
    page_number: int
    thumb_path: str
    snippet: str


_SEARCH_SQL = text("""
    SELECT
        magazines.id          AS magazine_id,
        magazines.title       AS magazine_title,
        pages.page_number     AS page_number,
        pages.thumb_path      AS thumb_path,
        snippet(pages_fts, 0, '<mark>', '</mark>', '…', 16) AS snippet
    FROM pages_fts
    JOIN pages     ON pages_fts.rowid = pages.id
    JOIN magazines ON pages.magazine_id = magazines.id
    WHERE pages_fts MATCH :q
    ORDER BY rank
    LIMIT :limit OFFSET :offset
""").bindparams(bindparam("q"), bindparam("limit"), bindparam("offset"))


def search(session: Session, raw_query: str, *, offset: int, limit: int) -> list[SearchResult]:
    q = sanitize_query(raw_query)
    if not q:
        return []
    try:
        rows = session.execute(_SEARCH_SQL, {"q": q, "limit": limit, "offset": offset}).all()
    except Exception:
        return []
    return [
        SearchResult(
            magazine_id=r.magazine_id,
            magazine_title=r.magazine_title,
            page_number=r.page_number,
            thumb_path=r.thumb_path,
            snippet=r.snippet,
        )
        for r in rows
    ]
