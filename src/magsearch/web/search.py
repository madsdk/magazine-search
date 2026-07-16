import re
from dataclasses import dataclass
from datetime import date

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


_SAFE_WORD = re.compile(r"[A-Za-z0-9]+")

_MIN_YEAR = 1000
_MAX_YEAR = 9999


def _parse_year(raw: object) -> int | None:
    try:
        year = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if year < _MIN_YEAR or year > _MAX_YEAR:
        return None
    return year


def coerce_year_range(
    raw_from: object, raw_to: object
) -> tuple[int | None, int | None]:
    """Parse/validate two year inputs. Non-numeric or out-of-range → None.
    If both are set and from > to, swap them. Never raises."""
    year_from = _parse_year(raw_from)
    year_to = _parse_year(raw_to)
    if year_from is not None and year_to is not None and year_from > year_to:
        year_from, year_to = year_to, year_from
    return year_from, year_to


def sanitize_query(
    raw: str,
    *,
    match_all: bool = True,
    match_phrase: bool = False,
) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    # Balance double quotes: if odd count, drop them all.
    if raw.count('"') % 2 != 0:
        raw = raw.replace('"', "")

    # Top-level parts (bare words and "quoted phrases"), plus the flat list
    # of plain words for phrase-mode reassembly.
    parts: list[str] = []
    plain_words: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == '"':
            close = raw.find('"', i + 1)
            if close == -1:
                break
            words = _SAFE_WORD.findall(raw[i + 1:close])
            if words:
                parts.append('"' + " ".join(words) + '"')
                plain_words.extend(words)
            i = close + 1
        else:
            m = _SAFE_WORD.match(raw, i)
            if m:
                parts.append(m.group(0))
                plain_words.append(m.group(0))
                i = m.end()
            else:
                i += 1

    if match_phrase:
        if not plain_words:
            return ""
        return '"' + " ".join(plain_words) + '"'
    if not parts:
        return ""
    if match_all:
        return " ".join(parts)
    return " OR ".join(parts)


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


# ─────────────────────────── sort whitelists ───────────────────────────
# ORDER BY can't be bind-parameterized, so we pre-build one text() per
# allowed sort key and look it up by validated enum value.

FLAT_SORT_OPTIONS = ("rank", "newest", "oldest")
GROUPED_SORT_OPTIONS = ("rank", "newest", "oldest", "matches")
PER_ISSUE_SORT_OPTIONS = ("rank", "page")
DEFAULT_FLAT_SORT = "rank"
DEFAULT_PER_ISSUE_SORT = "rank"


_FLAT_ORDER_CLAUSES = {
    "rank":   "rank, magazines.publication_date ASC, magazines.title, pages.page_number",
    "newest": "magazines.publication_date DESC, rank, pages.page_number",
    "oldest": "magazines.publication_date ASC, rank, pages.page_number",
}

_PER_TITLE_ORDER_CLAUSES = _FLAT_ORDER_CLAUSES  # same shape

_PER_ISSUE_ORDER_CLAUSES = {
    "rank": "rank, pages.page_number",
    "page": "pages.page_number",
}

_GROUPED_ORDER_CLAUSES = {
    "rank":    "best_rank, MIN(magazines.publication_date) ASC, magazines.title",
    "newest":  "MAX(magazines.publication_date) DESC, best_rank",
    "oldest":  "MIN(magazines.publication_date) ASC, best_rank",
    "matches": "COUNT(*) DESC, best_rank, magazines.id",
}


def _build_flat_sql() -> dict[str, "text"]:
    base = """
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
        ORDER BY {clause}
        LIMIT :limit OFFSET :offset
    """
    return {
        sort: text(base.format(clause=clause)).bindparams(
            bindparam("q"), bindparam("limit"), bindparam("offset")
        )
        for sort, clause in _FLAT_ORDER_CLAUSES.items()
    }


def _build_per_issue_sql() -> dict[str, "text"]:
    base = """
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
        ORDER BY {clause}
        LIMIT :limit OFFSET :offset
    """
    return {
        sort: text(base.format(clause=clause)).bindparams(
            bindparam("q"),
            bindparam("magazine_id"),
            bindparam("limit"),
            bindparam("offset"),
        )
        for sort, clause in _PER_ISSUE_ORDER_CLAUSES.items()
    }


def _build_per_title_sql() -> dict[str, "text"]:
    base = """
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
        WHERE pages_fts MATCH :q AND magazines.title = :title
        ORDER BY {clause}
        LIMIT :limit OFFSET :offset
    """
    return {
        sort: text(base.format(clause=clause)).bindparams(
            bindparam("q"),
            bindparam("title"),
            bindparam("limit"),
            bindparam("offset"),
        )
        for sort, clause in _PER_TITLE_ORDER_CLAUSES.items()
    }


def _build_grouped_sql() -> dict[str, "text"]:
    base = """
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
        ORDER BY {clause}
        LIMIT :limit OFFSET :offset
    """
    return {
        sort: text(base.format(clause=clause)).bindparams(
            bindparam("q"), bindparam("limit"), bindparam("offset")
        )
        for sort, clause in _GROUPED_ORDER_CLAUSES.items()
    }


_FLAT_SQL_BY_SORT = _build_flat_sql()
_PER_ISSUE_SQL_BY_SORT = _build_per_issue_sql()
_PER_TITLE_SQL_BY_SORT = _build_per_title_sql()
_GROUPED_SQL_BY_SORT = _build_grouped_sql()


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


def _pick(d: dict, key: str, default: str):
    return d.get(key, d[default])


def search(
    session: Session,
    raw_query: str,
    *,
    offset: int,
    limit: int,
    sort: str = DEFAULT_FLAT_SORT,
    match_all: bool = True,
    match_phrase: bool = False,
) -> list[SearchResult]:
    q = sanitize_query(raw_query, match_all=match_all, match_phrase=match_phrase)
    if not q:
        return []
    stmt = _pick(_FLAT_SQL_BY_SORT, sort, DEFAULT_FLAT_SORT)
    try:
        rows = session.execute(stmt, {"q": q, "limit": limit, "offset": offset}).all()
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
    sort: str = DEFAULT_PER_ISSUE_SORT,
    match_all: bool = True,
    match_phrase: bool = False,
) -> list[SearchResult]:
    q = sanitize_query(raw_query, match_all=match_all, match_phrase=match_phrase)
    if not q:
        return []
    stmt = _pick(_PER_ISSUE_SQL_BY_SORT, sort, DEFAULT_PER_ISSUE_SORT)
    try:
        rows = session.execute(
            stmt,
            {"q": q, "magazine_id": magazine_id, "limit": limit, "offset": offset},
        ).all()
    except Exception:
        return []
    return [_row_to_result(r) for r in rows]


def search_in_magazine_title(
    session: Session,
    raw_query: str,
    title: str,
    *,
    offset: int,
    limit: int,
    sort: str = DEFAULT_FLAT_SORT,
    match_all: bool = True,
    match_phrase: bool = False,
) -> list[SearchResult]:
    q = sanitize_query(raw_query, match_all=match_all, match_phrase=match_phrase)
    if not q:
        return []
    stmt = _pick(_PER_TITLE_SQL_BY_SORT, sort, DEFAULT_FLAT_SORT)
    try:
        rows = session.execute(
            stmt,
            {"q": q, "title": title, "limit": limit, "offset": offset},
        ).all()
    except Exception:
        return []
    return [_row_to_result(r) for r in rows]


def search_magazines(
    session: Session,
    raw_query: str,
    *,
    offset: int,
    limit: int,
    sort: str = DEFAULT_FLAT_SORT,
    match_all: bool = True,
    match_phrase: bool = False,
) -> list[MagazineMatch]:
    q = sanitize_query(raw_query, match_all=match_all, match_phrase=match_phrase)
    if not q:
        return []
    stmt = _pick(_GROUPED_SQL_BY_SORT, sort, DEFAULT_FLAT_SORT)
    try:
        rows = session.execute(
            stmt, {"q": q, "limit": limit, "offset": offset}
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
