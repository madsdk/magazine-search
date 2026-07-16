# Search Year-Range Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users restrict `/search` results to magazines published within an optional year range (From year, To year, both, or neither).

**Architecture:** Add a pure coercion helper and two always-present date bind-params to the flat and grouped FTS SQL. The `search()` and `search_magazines()` functions gain optional `year_from`/`year_to` ints; the `/search` route coerces raw query params through the helper and threads the active range through the template's view/sort/pagination links.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy Core `text()`, SQLite FTS5, Jinja2, pytest.

## Global Constraints

- Search must never raise on bad input — bad years coerce to blank, never a 422 or 500 (mirrors existing FTS behavior where an unparseable query returns `[]`).
- Scope is `/search` only (grouped + flat views). Do **not** modify `search_in_magazine`, `search_in_magazine_title`, `_build_per_issue_sql`, or `_build_per_title_sql`.
- `publication_date` is stored by SQLite as ISO `YYYY-MM-DD` text; year bounds become `f"{year:04d}-01-01"` (from) and `f"{year:04d}-12-31"` (to), so lexicographic comparison equals chronological comparison.
- Undated issues (`publication_date IS NULL`) must be excluded whenever either bound is set, and included when neither is — achieved for free by SQL three-valued logic (`NULL >= '1985-01-01'` is falsy).
- Year sanity clamp: accept `1000`–`9999` inclusive; anything outside → treated as blank.

---

### Task 1: `coerce_year_range` helper

Pure input-validation function. No DB, no SQL.

**Files:**
- Modify: `src/magsearch/web/search.py` (add helpers near `sanitize_query`, top of file after the `_SAFE_WORD` constant)
- Test: `tests/test_search.py` (add tests near the existing `sanitize_query` tests)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `coerce_year_range(raw_from: object, raw_to: object) -> tuple[int | None, int | None]` — parses/validates two year inputs; non-numeric or out-of-range → `None`; if both set and `from > to`, swaps them; never raises.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_search.py` (import is already present: `from magsearch.web.search import ... ` — extend it to include `coerce_year_range`):

```python
from magsearch.web.search import coerce_year_range  # add to existing import block


def test_coerce_year_range_parses_valid_years():
    assert coerce_year_range("1980", "1985") == (1980, 1985)
    assert coerce_year_range(1980, 1985) == (1980, 1985)
    assert coerce_year_range(" 1980 ", " 1985 ") == (1980, 1985)


def test_coerce_year_range_blank_bounds_are_none():
    assert coerce_year_range("", "") == (None, None)
    assert coerce_year_range("1980", "") == (1980, None)
    assert coerce_year_range("", "1985") == (None, 1985)
    assert coerce_year_range(None, None) == (None, None)


def test_coerce_year_range_junk_is_none():
    assert coerce_year_range("abc", "19x5") == (None, None)
    assert coerce_year_range("1980.0", "nineteen") == (None, None)


def test_coerce_year_range_out_of_range_is_none():
    assert coerce_year_range("0", "20260") == (None, None)
    assert coerce_year_range("999", "10000") == (None, None)
    # Boundaries are inclusive.
    assert coerce_year_range("1000", "9999") == (1000, 9999)


def test_coerce_year_range_swaps_reversed():
    assert coerce_year_range("1985", "1980") == (1980, 1985)
    # Only swaps when BOTH are set.
    assert coerce_year_range("1985", "") == (1985, None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_search.py -k coerce_year_range -v`
Expected: FAIL — `ImportError: cannot import name 'coerce_year_range'`.

- [ ] **Step 3: Write the implementation**

In `src/magsearch/web/search.py`, after the `_SAFE_WORD = re.compile(...)` line, add:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_search.py -k coerce_year_range -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/magsearch/web/search.py tests/test_search.py
git commit -m "feat: coerce_year_range helper for search year filter"
```

---

### Task 2: Year filtering in `search()` and `search_magazines()`

Add the date bind-params to the flat and grouped SQL and the two functions.

**Files:**
- Modify: `src/magsearch/web/search.py` — `_build_flat_sql`, `_build_grouped_sql`, `search`, `search_magazines`; add `_date_bounds` helper.
- Test: `tests/test_search.py` (new fixture + tests)

**Interfaces:**
- Consumes: `coerce_year_range` (Task 1) is **not** used here — these functions receive already-parsed ints. Callers are responsible for coercion.
- Produces:
  - `search(session, raw_query, *, offset, limit, sort=..., match_all=True, match_phrase=False, year_from: int | None = None, year_to: int | None = None) -> list[SearchResult]`
  - `search_magazines(session, raw_query, *, offset, limit, sort=..., match_all=True, match_phrase=False, year_from: int | None = None, year_to: int | None = None) -> list[MagazineMatch]`
  - `_date_bounds(year_from: int | None, year_to: int | None) -> tuple[str | None, str | None]` (module-private).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_search.py`. First a new fixture (place it after the existing `two_magazines_db` fixture), then the tests. The three magazines all contain "synthesizer"; distinct `page_text` per PDF guarantees distinct content hashes so none are deduped by the content-hash guard.

```python
@pytest.fixture
def dated_corpus_db(tmp_path, monkeypatch):
    """Three magazines all matching 'synthesizer': two dated, one undated."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    bundles = tmp_path / "bundles"

    byte = IngestPipeline(
        bundles, FakeOCREngine(responses=[
            [OCRRegion(text="vintage synthesizer review", bbox=(0,0,50,10), confidence=1.0)],
        ]),
        IngestOptions(title="Byte", publication_date=date(1985, 12, 1)),
    ).run(make_pdf(tmp_path / "byte.pdf", num_pages=1, page_text=["byte"]))

    compute = IngestPipeline(
        bundles, FakeOCREngine(responses=[
            [OCRRegion(text="synthesizer programming tutorial", bbox=(0,0,50,10), confidence=1.0)],
        ]),
        IngestOptions(title="Compute", publication_date=date(1984, 6, 1)),
    ).run(make_pdf(tmp_path / "compute.pdf", num_pages=1, page_text=["compute"]))

    retro = IngestPipeline(
        bundles, FakeOCREngine(responses=[
            [OCRRegion(text="synthesizer sounds sampled", bbox=(0,0,50,10), confidence=1.0)],
        ]),
        IngestOptions(title="Retro", publication_date=None),
    ).run(make_pdf(tmp_path / "retro.pdf", num_pages=1, page_text=["retro"]))

    with session_scope(factory) as s:
        import_bundle(byte.bundle_dir, s)
        import_bundle(compute.bundle_dir, s)
        import_bundle(retro.bundle_dir, s)
    return factory


def _titles(results):
    return {r.magazine_title for r in results}


def test_search_no_year_filter_includes_undated(dated_corpus_db):
    with session_scope(dated_corpus_db) as s:
        results = search(s, "synthesizer", offset=0, limit=10)
    assert _titles(results) == {"Byte", "Compute", "Retro"}


def test_search_year_from_only(dated_corpus_db):
    with session_scope(dated_corpus_db) as s:
        results = search(s, "synthesizer", offset=0, limit=10, year_from=1985)
    # Byte (1985) only; Compute (1984) below, Retro undated excluded.
    assert _titles(results) == {"Byte"}


def test_search_year_to_only(dated_corpus_db):
    with session_scope(dated_corpus_db) as s:
        results = search(s, "synthesizer", offset=0, limit=10, year_to=1984)
    assert _titles(results) == {"Compute"}


def test_search_year_closed_range_excludes_undated(dated_corpus_db):
    with session_scope(dated_corpus_db) as s:
        results = search(s, "synthesizer", offset=0, limit=10,
                         year_from=1984, year_to=1985)
    assert _titles(results) == {"Byte", "Compute"}


def test_search_magazines_year_filter(dated_corpus_db):
    with session_scope(dated_corpus_db) as s:
        hits = search_magazines(s, "synthesizer", offset=0, limit=10,
                                year_from=1984, year_to=1985)
    assert {h.magazine_title for h in hits} == {"Byte", "Compute"}


def test_search_magazines_year_filter_excludes_undated(dated_corpus_db):
    with session_scope(dated_corpus_db) as s:
        hits = search_magazines(s, "synthesizer", offset=0, limit=10,
                                year_from=1980)
    assert "Retro" not in {h.magazine_title for h in hits}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_search.py -k "dated_corpus or year_from or year_to or year_filter or closed_range or includes_undated" -v`
Expected: FAIL — `TypeError: search() got an unexpected keyword argument 'year_from'`.

- [ ] **Step 3a: Add the `_date_bounds` helper**

In `src/magsearch/web/search.py`, add near the other module-level helpers (e.g. just above `_coerce_date`):

```python
def _date_bounds(
    year_from: int | None, year_to: int | None
) -> tuple[str | None, str | None]:
    date_from = f"{year_from:04d}-01-01" if year_from is not None else None
    date_to = f"{year_to:04d}-12-31" if year_to is not None else None
    return date_from, date_to
```

- [ ] **Step 3b: Add the date clauses to the flat SQL**

In `_build_flat_sql`, change the `WHERE` block and the `bindparams(...)` call. Replace:

```python
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
```

with:

```python
        WHERE pages_fts MATCH :q
          AND (:date_from IS NULL OR magazines.publication_date >= :date_from)
          AND (:date_to   IS NULL OR magazines.publication_date <= :date_to)
        ORDER BY {clause}
        LIMIT :limit OFFSET :offset
    """
    return {
        sort: text(base.format(clause=clause)).bindparams(
            bindparam("q"), bindparam("limit"), bindparam("offset"),
            bindparam("date_from"), bindparam("date_to"),
        )
        for sort, clause in _FLAT_ORDER_CLAUSES.items()
    }
```

- [ ] **Step 3c: Add the date clauses to the grouped SQL**

In `_build_grouped_sql`, replace:

```python
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
```

with:

```python
        WHERE pages_fts MATCH :q
          AND (:date_from IS NULL OR magazines.publication_date >= :date_from)
          AND (:date_to   IS NULL OR magazines.publication_date <= :date_to)
        GROUP BY magazines.id
        ORDER BY {clause}
        LIMIT :limit OFFSET :offset
    """
    return {
        sort: text(base.format(clause=clause)).bindparams(
            bindparam("q"), bindparam("limit"), bindparam("offset"),
            bindparam("date_from"), bindparam("date_to"),
        )
        for sort, clause in _GROUPED_ORDER_CLAUSES.items()
    }
```

- [ ] **Step 3d: Thread the params through `search()`**

Replace the `search(...)` function body's signature and execute call. New version:

```python
def search(
    session: Session,
    raw_query: str,
    *,
    offset: int,
    limit: int,
    sort: str = DEFAULT_FLAT_SORT,
    match_all: bool = True,
    match_phrase: bool = False,
    year_from: int | None = None,
    year_to: int | None = None,
) -> list[SearchResult]:
    q = sanitize_query(raw_query, match_all=match_all, match_phrase=match_phrase)
    if not q:
        return []
    date_from, date_to = _date_bounds(year_from, year_to)
    stmt = _pick(_FLAT_SQL_BY_SORT, sort, DEFAULT_FLAT_SORT)
    try:
        rows = session.execute(
            stmt,
            {"q": q, "limit": limit, "offset": offset,
             "date_from": date_from, "date_to": date_to},
        ).all()
    except Exception:
        return []
    return [_row_to_result(r) for r in rows]
```

- [ ] **Step 3e: Thread the params through `search_magazines()`**

Replace the `search_magazines(...)` signature and execute call. New version:

```python
def search_magazines(
    session: Session,
    raw_query: str,
    *,
    offset: int,
    limit: int,
    sort: str = DEFAULT_FLAT_SORT,
    match_all: bool = True,
    match_phrase: bool = False,
    year_from: int | None = None,
    year_to: int | None = None,
) -> list[MagazineMatch]:
    q = sanitize_query(raw_query, match_all=match_all, match_phrase=match_phrase)
    if not q:
        return []
    date_from, date_to = _date_bounds(year_from, year_to)
    stmt = _pick(_GROUPED_SQL_BY_SORT, sort, DEFAULT_FLAT_SORT)
    try:
        rows = session.execute(
            stmt,
            {"q": q, "limit": limit, "offset": offset,
             "date_from": date_from, "date_to": date_to},
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
```

- [ ] **Step 4: Run the new tests + the full search suite (no regressions)**

Run: `pytest tests/test_search.py -v`
Expected: PASS — all existing tests still pass (they pass `year_from`/`year_to` as their `None` defaults) plus the 6 new year tests.

- [ ] **Step 5: Commit**

```bash
git add src/magsearch/web/search.py tests/test_search.py
git commit -m "feat: year-range filtering in search() and search_magazines()"
```

---

### Task 3: Wire year range into the `/search` route and form

**Files:**
- Modify: `src/magsearch/web/routes.py` — `search_route`
- Modify: `src/magsearch/web/templates/search.html` — `search_url` macro + form inputs
- Test: `tests/test_web_search.py`

**Interfaces:**
- Consumes: `coerce_year_range` (Task 1); `search` / `search_magazines` `year_from`/`year_to` params (Task 2).
- Produces: `/search` accepts `year_from` and `year_to` query params (strings); template context gains `year_from` / `year_to` (coerced `int | None`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web_search.py`. The `app_client` corpus has Byte (1985-12) and Compute (1984-06), both matching "synthesizer".

```python
def test_search_year_filter_narrows_results(app_client):
    client, _ = app_client
    # Flat view, restrict to 1985 → Byte only, Compute (1984) excluded.
    resp = client.get("/search", params={
        "q": "synthesizer", "view": "flat", "year_from": "1985",
    })
    assert resp.status_code == 200
    assert "Byte" in resp.text
    assert "Compute" not in resp.text


def test_search_year_to_filters_out_newer(app_client):
    client, _ = app_client
    resp = client.get("/search", params={
        "q": "synthesizer", "view": "flat", "year_to": "1984",
    })
    assert resp.status_code == 200
    assert "Compute" in resp.text
    assert "Byte" not in resp.text


def test_search_bad_year_does_not_500(app_client):
    client, _ = app_client
    resp = client.get("/search", params={
        "q": "synthesizer", "view": "flat", "year_from": "abc", "year_to": "99999",
    })
    # Junk coerces to blank → behaves as no filter, both magazines return.
    assert resp.status_code == 200
    assert "Byte" in resp.text
    assert "Compute" in resp.text


def test_search_year_filter_prefills_form(app_client):
    client, _ = app_client
    resp = client.get("/search", params={"q": "synthesizer", "year_from": "1980"})
    assert resp.status_code == 200
    assert 'name="year_from"' in resp.text
    assert 'value="1980"' in resp.text


def test_search_year_filter_persists_across_view_toggle(app_client):
    client, _ = app_client
    # Grouped view offers a "show all matching pages" link to flat view;
    # that link must carry the active year filter.
    resp = client.get("/search", params={
        "q": "synthesizer", "year_from": "1984", "year_to": "1985",
    })
    assert resp.status_code == 200
    assert "year_from=1984" in resp.text
    assert "year_to=1985" in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web_search.py -k year -v`
Expected: FAIL — the year params are ignored (both magazines returned; no `name="year_from"` in output).

- [ ] **Step 3a: Update the route**

In `src/magsearch/web/routes.py`, add the import (extend the existing `from magsearch.web.search import (...)` block) with `coerce_year_range`.

In `search_route`, add two query params to the signature (after `match_phrase`):

```python
    year_from: str = Query(default=""),
    year_to: str = Query(default=""),
```

After the line `match_all_b = bool(match_all) or match_phrase_b`, add:

```python
    year_from_i, year_to_i = coerce_year_range(year_from, year_to)
```

Update **both** search calls to pass the years. The flat branch:

```python
    if view == "flat":
        results = search(
            db, q, offset=offset, limit=per_page + 1, sort=sort,
            match_all=match_all_b, match_phrase=match_phrase_b,
            year_from=year_from_i, year_to=year_to_i,
        )
    else:
        results = search_magazines(
            db, q, offset=offset, limit=per_page + 1, sort=sort,
            match_all=match_all_b, match_phrase=match_phrase_b,
            year_from=year_from_i, year_to=year_to_i,
        )
```

Add the coerced values to the template context dict (alongside `match_all`/`match_phrase`):

```python
            "year_from": year_from_i,
            "year_to": year_to_i,
```

- [ ] **Step 3b: Update the `search_url` macro in `search.html`**

Replace the macro (lines 6–10) with a version that threads the year range:

```jinja
{%- macro search_url(view_=None, sort_=None, per_page_=None, page_=1, match_all_=None, match_phrase_=None) -%}
{%- set ma = match_all_ if match_all_ is not none else (1 if match_all else 0) -%}
{%- set mp = match_phrase_ if match_phrase_ is not none else (1 if match_phrase else 0) -%}
/search?q={{ q|urlencode }}&view={{ view_ or view }}&sort={{ sort_ or sort }}&per_page={{ per_page_ or per_page }}&match_all={{ ma }}&match_phrase={{ mp }}&year_from={{ year_from or '' }}&year_to={{ year_to or '' }}&page={{ page_ }}
{%- endmacro -%}
```

- [ ] **Step 3c: Add the year inputs to the form in `search.html`**

Inside `<form action="/search" method="get">`, the `view`/`sort`/`per_page` hidden inputs already preserve display prefs. Add the two year inputs inside the options row (the `<div class="mt-3 caps caps-soft ...">` block), after the `match_phrase` label's closing `</label>` and before the closing `</div>`:

```jinja
      <label class="flex items-center gap-2">
        <span>from year</span>
        <input type="number" name="year_from" value="{{ year_from or '' }}"
               placeholder="—" inputmode="numeric"
               class="field-editorial w-20">
      </label>
      <label class="flex items-center gap-2">
        <span>to year</span>
        <input type="number" name="year_to" value="{{ year_to or '' }}"
               placeholder="—" inputmode="numeric"
               class="field-editorial w-20">
      </label>
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_web_search.py -k year -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the full web + search suites (no regressions)**

Run: `pytest tests/test_web_search.py tests/test_search.py -v`
Expected: PASS — all existing and new tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/magsearch/web/routes.py src/magsearch/web/templates/search.html tests/test_web_search.py
git commit -m "feat: year-range filter on /search route and form"
```

---

## Final verification

- [ ] **Run the whole suite**

Run: `pytest -q`
Expected: all pass (the baseline is "151 passed, 1 skipped, 1 deselected" plus the new tests — no failures, no new skips).

- [ ] **Manual smoke test (optional)**

```bash
magsearch web --reload
# Search a term, set "from year" / "to year", confirm results narrow and the
# range survives toggling grouped ↔ flat and paging.
```

## Self-review notes

- **Spec coverage:** year-range semantics + open bounds (Task 2 tests), undated exclusion/inclusion (Task 2 tests), robustness/coercion/swap (Task 1 tests), scope = `/search` only (Tasks 2 & 3 leave per-issue/per-title SQL untouched), UI threading across view toggle (Task 3 `test_search_year_filter_persists_across_view_toggle`), prefill (Task 3 `test_search_year_filter_prefills_form`) — all covered.
- **Type consistency:** `year_from`/`year_to` are `int | None` in `search`/`search_magazines` and in the template context; raw route params are `str`; `coerce_year_range` bridges `str → int | None`. Boundary strings are produced only inside `_date_bounds`.
- **No placeholders:** every code and test step contains complete content.
