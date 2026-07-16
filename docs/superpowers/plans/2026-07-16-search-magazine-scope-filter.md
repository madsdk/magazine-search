# Search Magazine-Scope Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users restrict `/search` to a chosen subset of magazine titles via an on-demand checkbox panel (all titles checked by default, with check-all / check-none).

**Architecture:** Add an optional `titles` filter to the flat/grouped search functions (dynamic `title IN :titles` via an expanding bind param; empty custom selection short-circuits to `[]`). The `/search` route lists distinct titles, parses a `mag_scope`/`mag` selection, and threads it through the template. An inline `<details>` panel renders the checkboxes; a small vanilla-JS addition handles the buttons and submit normalization.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy Core `text()` with expanding bind params, SQLite FTS5, Jinja2, vanilla JS.

## Global Constraints

- Search must never raise on bad input: `mag_scope`/`mag` accepted as `str`/`list[str]`; unknown titles are silently dropped; never a 422 or 500.
- Empty custom selection (`mag_scope=custom`, zero valid titles) → **no results**, short-circuited in Python before any DB query (no invalid `IN ()`).
- Default (all titles selected) → **no title filter** and a clean URL (no `mag_scope`/`mag` params).
- Title `IN` must use a SQLAlchemy **expanding** bind param — never string interpolation of user values.
- Scope is `/search` only (flat + grouped). Do NOT modify `search_in_magazine`, `search_in_magazine_title`, `_build_per_issue_sql`, or `_build_per_title_sql`.
- The active selection (`mag_scope=custom` + one `mag=<title>` per selected title) must persist across every view/sort/per-page/pagination link, exactly as `match_all`/`match_phrase`/`year_from`/`year_to` already do.
- Reuse existing utility classes and the existing `base.html` vanilla-JS pattern; do not add a framework or new build step (Tailwind is precompiled to a static file — only use utility classes already present, and put new CSS in the hand-written `<style>` block in `base.html`).

---

### Task 1: Optional `titles` filter in `search()` / `search_magazines()`

Refactor the flat + grouped base SQL into shared constants, prebuild a title-filtered variant, and add the `titles` parameter.

**Files:**
- Modify: `src/magsearch/web/search.py`
- Test: `tests/test_search.py`

**Interfaces:**
- Consumes: existing `_FLAT_ORDER_CLAUSES`, `_GROUPED_ORDER_CLAUSES`, `_date_bounds`, `_pick`.
- Produces:
  - `search(session, raw_query, *, offset, limit, sort=..., match_all=True, match_phrase=False, year_from=None, year_to=None, titles: list[str] | None = None) -> list[SearchResult]`
  - `search_magazines(session, raw_query, *, offset, limit, sort=..., match_all=True, match_phrase=False, year_from=None, year_to=None, titles: list[str] | None = None) -> list[MagazineMatch]`
  - Semantics: `titles is None` → no filter; `titles == []` → `[]`; `titles == [..]` → restrict to those titles.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_search.py` (the `dated_corpus_db` fixture already defines three titles — Byte 1985, Compute 1984, Retro undated — all matching "synthesizer"; `_titles` helper already exists):

```python
def test_search_titles_none_searches_all(dated_corpus_db):
    with session_scope(dated_corpus_db) as s:
        results = search(s, "synthesizer", offset=0, limit=10, titles=None)
    assert _titles(results) == {"Byte", "Compute", "Retro"}


def test_search_titles_single(dated_corpus_db):
    with session_scope(dated_corpus_db) as s:
        results = search(s, "synthesizer", offset=0, limit=10, titles=["Byte"])
    assert _titles(results) == {"Byte"}


def test_search_titles_subset(dated_corpus_db):
    with session_scope(dated_corpus_db) as s:
        results = search(s, "synthesizer", offset=0, limit=10,
                         titles=["Byte", "Compute"])
    assert _titles(results) == {"Byte", "Compute"}


def test_search_titles_empty_returns_nothing(dated_corpus_db):
    with session_scope(dated_corpus_db) as s:
        results = search(s, "synthesizer", offset=0, limit=10, titles=[])
    assert results == []


def test_search_magazines_titles_subset(dated_corpus_db):
    with session_scope(dated_corpus_db) as s:
        hits = search_magazines(s, "synthesizer", offset=0, limit=10,
                                titles=["Byte"])
    assert {h.magazine_title for h in hits} == {"Byte"}


def test_search_titles_combines_with_year(dated_corpus_db):
    # Byte(1985) + Compute(1984) selected, but year_from=1985 drops Compute.
    with session_scope(dated_corpus_db) as s:
        results = search(s, "synthesizer", offset=0, limit=10,
                         titles=["Byte", "Compute"], year_from=1985)
    assert _titles(results) == {"Byte"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_search.py -k "titles" -v`
Expected: FAIL — `TypeError: search() got an unexpected keyword argument 'titles'`.

- [ ] **Step 3a: Extract shared base-SQL constants**

In `src/magsearch/web/search.py`, add these module-level constants just above `def _build_flat_sql`:

```python
_FLAT_BASE_SQL = """
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
      AND (:date_from IS NULL OR magazines.publication_date >= :date_from)
      AND (:date_to   IS NULL OR magazines.publication_date <= :date_to)
      {title_clause}
    ORDER BY {clause}
    LIMIT :limit OFFSET :offset
"""

_GROUPED_BASE_SQL = """
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
      AND (:date_from IS NULL OR magazines.publication_date >= :date_from)
      AND (:date_to   IS NULL OR magazines.publication_date <= :date_to)
      {title_clause}
    GROUP BY magazines.id
    ORDER BY {clause}
    LIMIT :limit OFFSET :offset
"""

_TITLE_CLAUSE = "AND magazines.title IN :titles"
```

- [ ] **Step 3b: Rewrite `_build_flat_sql` and `_build_grouped_sql` to take a `title_filter` flag**

Replace the existing `def _build_flat_sql() -> dict[str, "text"]:` function (the whole function, including its local `base`) with:

```python
def _build_flat_sql(*, title_filter: bool) -> dict[str, "text"]:
    title_clause = _TITLE_CLAUSE if title_filter else ""
    params = [
        bindparam("q"), bindparam("limit"), bindparam("offset"),
        bindparam("date_from"), bindparam("date_to"),
    ]
    if title_filter:
        params.append(bindparam("titles", expanding=True))
    return {
        sort: text(
            _FLAT_BASE_SQL.format(clause=clause, title_clause=title_clause)
        ).bindparams(*params)
        for sort, clause in _FLAT_ORDER_CLAUSES.items()
    }
```

Replace the existing `def _build_grouped_sql() -> dict[str, "text"]:` function (the whole function, including its local `base`) with:

```python
def _build_grouped_sql(*, title_filter: bool) -> dict[str, "text"]:
    title_clause = _TITLE_CLAUSE if title_filter else ""
    params = [
        bindparam("q"), bindparam("limit"), bindparam("offset"),
        bindparam("date_from"), bindparam("date_to"),
    ]
    if title_filter:
        params.append(bindparam("titles", expanding=True))
    return {
        sort: text(
            _GROUPED_BASE_SQL.format(clause=clause, title_clause=title_clause)
        ).bindparams(*params)
        for sort, clause in _GROUPED_ORDER_CLAUSES.items()
    }
```

Leave `_build_per_issue_sql` and `_build_per_title_sql` exactly as they are.

- [ ] **Step 3c: Prebuild both variants**

Replace the module-level build lines:

```python
_FLAT_SQL_BY_SORT = _build_flat_sql()
_PER_ISSUE_SQL_BY_SORT = _build_per_issue_sql()
_PER_TITLE_SQL_BY_SORT = _build_per_title_sql()
_GROUPED_SQL_BY_SORT = _build_grouped_sql()
```

with:

```python
_FLAT_SQL_BY_SORT = _build_flat_sql(title_filter=False)
_FLAT_SQL_TITLE_BY_SORT = _build_flat_sql(title_filter=True)
_PER_ISSUE_SQL_BY_SORT = _build_per_issue_sql()
_PER_TITLE_SQL_BY_SORT = _build_per_title_sql()
_GROUPED_SQL_BY_SORT = _build_grouped_sql(title_filter=False)
_GROUPED_SQL_TITLE_BY_SORT = _build_grouped_sql(title_filter=True)
```

- [ ] **Step 3d: Thread `titles` through `search()`**

Replace the `search(...)` function with:

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
    titles: list[str] | None = None,
) -> list[SearchResult]:
    q = sanitize_query(raw_query, match_all=match_all, match_phrase=match_phrase)
    if not q:
        return []
    if titles is not None and len(titles) == 0:
        return []
    date_from, date_to = _date_bounds(year_from, year_to)
    params = {
        "q": q, "limit": limit, "offset": offset,
        "date_from": date_from, "date_to": date_to,
    }
    if titles is None:
        stmt = _pick(_FLAT_SQL_BY_SORT, sort, DEFAULT_FLAT_SORT)
    else:
        stmt = _pick(_FLAT_SQL_TITLE_BY_SORT, sort, DEFAULT_FLAT_SORT)
        params["titles"] = titles
    try:
        rows = session.execute(stmt, params).all()
    except Exception:
        return []
    return [_row_to_result(r) for r in rows]
```

- [ ] **Step 3e: Thread `titles` through `search_magazines()`**

Replace the `search_magazines(...)` function with:

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
    titles: list[str] | None = None,
) -> list[MagazineMatch]:
    q = sanitize_query(raw_query, match_all=match_all, match_phrase=match_phrase)
    if not q:
        return []
    if titles is not None and len(titles) == 0:
        return []
    date_from, date_to = _date_bounds(year_from, year_to)
    params = {
        "q": q, "limit": limit, "offset": offset,
        "date_from": date_from, "date_to": date_to,
    }
    if titles is None:
        stmt = _pick(_GROUPED_SQL_BY_SORT, sort, DEFAULT_FLAT_SORT)
    else:
        stmt = _pick(_GROUPED_SQL_TITLE_BY_SORT, sort, DEFAULT_FLAT_SORT)
        params["titles"] = titles
    try:
        rows = session.execute(stmt, params).all()
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

(Confirm `bindparam` is already imported at the top of the file — it is: `from sqlalchemy import bindparam, text`. `expanding=True` is a keyword of `bindparam`, no new import needed.)

- [ ] **Step 4: Run the new tests + the full search suite**

Run: `python -m pytest tests/test_search.py -v`
Expected: PASS — the 6 new `titles` tests plus all existing search tests (the SQL refactor is output-identical for the no-title path).

- [ ] **Step 5: Commit**

```bash
git add src/magsearch/web/search.py tests/test_search.py
git commit -m "feat: optional title filter in search() and search_magazines()"
```

---

### Task 2: `/search` route — title list, scope parsing, filtered search

**Files:**
- Modify: `src/magsearch/web/routes.py` — `search_route`
- Test: `tests/test_web_search.py`

**Interfaces:**
- Consumes: `search` / `search_magazines` `titles` param (Task 1); `Magazine` model (already imported); `select` (already imported).
- Produces: `/search` accepts `mag_scope: str` and repeated `mag: list[str]`; template context gains `all_titles: list[str]`, `mag_scope: str` (normalized `"custom"` or `""`), `selected_titles: set[str]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web_search.py`. The `app_client` corpus is Byte (`byte-1985-12`) and Compute (`compute-1984-06`), both matching "synthesizer". Assertions key on magazine **IDs** (which appear only in result links, never in the title-based panel added in Task 3) so they stay valid after the panel exists.

```python
def test_search_scope_custom_single_title(app_client):
    client, _ = app_client
    resp = client.get("/search", params={
        "q": "synthesizer", "mag_scope": "custom", "mag": "Byte",
    })
    assert resp.status_code == 200
    assert "/magazine/byte-1985-12" in resp.text
    assert "/magazine/compute-1984-06" not in resp.text


def test_search_scope_default_searches_all(app_client):
    client, _ = app_client
    resp = client.get("/search", params={"q": "synthesizer"})
    assert resp.status_code == 200
    assert "/magazine/byte-1985-12" in resp.text
    assert "/magazine/compute-1984-06" in resp.text


def test_search_scope_custom_empty_returns_no_results(app_client):
    client, _ = app_client
    resp = client.get("/search", params={"q": "synthesizer", "mag_scope": "custom"})
    assert resp.status_code == 200
    assert "/magazine/byte-1985-12" not in resp.text
    assert "/magazine/compute-1984-06" not in resp.text


def test_search_scope_unknown_title_does_not_500(app_client):
    client, _ = app_client
    resp = client.get("/search", params={
        "q": "synthesizer", "mag_scope": "custom", "mag": "Nonexistent",
    })
    # Unknown title dropped → empty effective selection → no results, no crash.
    assert resp.status_code == 200
    assert "/magazine/byte-1985-12" not in resp.text
    assert "/magazine/compute-1984-06" not in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_web_search.py -k "scope" -v`
Expected: FAIL — the scope params are ignored, so `test_search_scope_custom_single_title` and `test_search_scope_custom_empty_returns_no_results` fail (Compute still present / both still present).

- [ ] **Step 3: Update the route**

In `src/magsearch/web/routes.py`, add two query params to `search_route`'s signature (after `year_to`):

```python
    mag_scope: str = Query(default=""),
    mag: list[str] = Query(default=[]),
```

After the line `year_from_i, year_to_i = coerce_year_range(year_from, year_to)`, add:

```python
    all_titles = list(
        db.scalars(select(Magazine.title).distinct().order_by(Magazine.title)).all()
    )
    if mag_scope == "custom":
        title_set = set(all_titles)
        selected_titles = [t for t in mag if t in title_set]
        search_titles: list[str] | None = selected_titles
        mag_scope_norm = "custom"
    else:
        selected_titles = all_titles
        search_titles = None
        mag_scope_norm = ""
```

Pass `titles=search_titles` into BOTH search calls:

```python
    if view == "flat":
        results = search(
            db, q, offset=offset, limit=per_page + 1, sort=sort,
            match_all=match_all_b, match_phrase=match_phrase_b,
            year_from=year_from_i, year_to=year_to_i,
            titles=search_titles,
        )
    else:
        results = search_magazines(
            db, q, offset=offset, limit=per_page + 1, sort=sort,
            match_all=match_all_b, match_phrase=match_phrase_b,
            year_from=year_from_i, year_to=year_to_i,
            titles=search_titles,
        )
```

Add to the template context dict (alongside `year_from`/`year_to`):

```python
            "all_titles": all_titles,
            "mag_scope": mag_scope_norm,
            "selected_titles": set(selected_titles),
```

- [ ] **Step 4: Run the scope tests + full web-search suite**

Run: `python -m pytest tests/test_web_search.py -v`
Expected: PASS — the 4 new scope tests plus all existing web-search tests.

- [ ] **Step 5: Commit**

```bash
git add src/magsearch/web/routes.py tests/test_web_search.py
git commit -m "feat: parse magazine-scope selection in /search route"
```

---

### Task 3: Template — panel, checkboxes, and link threading

**Files:**
- Modify: `src/magsearch/web/templates/search.html`
- Modify: `src/magsearch/web/templates/base.html` (add `.mag-grid` CSS to the `<style>` block)
- Test: `tests/test_web_search.py`

**Interfaces:**
- Consumes: `all_titles`, `mag_scope`, `selected_titles` (Task 2).
- Produces: rendered `<details>` panel with `name="mag"` checkboxes + hidden `name="mag_scope"`; `search_url` macro threads `mag_scope`/`mag`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_web_search.py`:

```python
def test_search_panel_lists_all_titles_checked_by_default(app_client):
    client, _ = app_client
    resp = client.get("/search", params={"q": "synthesizer"})
    assert resp.status_code == 200
    assert "limit to magazines" in resp.text
    assert 'value="Byte"' in resp.text
    assert 'value="Compute"' in resp.text
    # Default: every box checked.
    import re
    assert re.search(r'value="Byte"[^>]*\bchecked', resp.text)
    assert re.search(r'value="Compute"[^>]*\bchecked', resp.text)


def test_search_panel_reflects_custom_selection(app_client):
    client, _ = app_client
    resp = client.get("/search", params={
        "q": "synthesizer", "mag_scope": "custom", "mag": "Byte",
    })
    assert resp.status_code == 200
    import re
    # Byte checked, Compute not.
    assert re.search(r'value="Byte"[^>]*\bchecked', resp.text)
    assert not re.search(r'value="Compute"[^>]*\bchecked', resp.text)
    # Panel is open and shows the count.
    assert re.search(r"<details[^>]*\bopen", resp.text)
    assert "1 of 2" in resp.text


def test_search_scope_persists_across_view_toggle(app_client):
    client, _ = app_client
    resp = client.get("/search", params={
        "q": "synthesizer", "mag_scope": "custom", "mag": "Byte",
    })
    assert resp.status_code == 200
    # The grouped→flat toggle link carries the selection.
    assert "mag_scope=custom" in resp.text
    assert "mag=Byte" in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_web_search.py -k "panel or persists_across_view" -v`
Expected: FAIL — no panel markup yet (`limit to magazines` / `value="Byte"` absent).

- [ ] **Step 3a: Add `.mag-grid` CSS to `base.html`**

In `src/magsearch/web/templates/base.html`, inside the `<style>` block, add after the `.field-year` rules block:

```css
  /* Scrollable grid of magazine-scope checkboxes. */
  .mag-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(12rem, 1fr));
    gap: 0.25rem 1.5rem;
    max-height: 15rem;
    overflow-y: auto;
    padding-right: 0.5rem;
  }
```

- [ ] **Step 3b: Thread `mag_scope`/`mag` through the `search_url` macro**

In `src/magsearch/web/templates/search.html`, replace the macro body line (currently line 9, the `/search?...&page={{ page_ }}` line) with:

```jinja
/search?q={{ q|urlencode }}&view={{ view_ or view }}&sort={{ sort_ or sort }}&per_page={{ per_page_ or per_page }}&match_all={{ ma }}&match_phrase={{ mp }}&year_from={{ year_from or '' }}&year_to={{ year_to or '' }}{% if mag_scope == 'custom' %}&mag_scope=custom{% for t in all_titles if t in selected_titles %}&mag={{ t|urlencode }}{% endfor %}{% endif %}&page={{ page_ }}
```

- [ ] **Step 3c: Add the hidden scope input and the panel**

In `src/magsearch/web/templates/search.html`, add the hidden scope input alongside the other hidden inputs (after the `per_page` hidden input, currently line 36):

```jinja
    <input type="hidden" name="mag_scope" value="all">
```

Then, immediately after the closing `</div>` of the options row (the `<div class="mt-3 caps caps-soft flex flex-wrap ...">` block that ends just before `</form>`), add the panel:

```jinja
    <details class="mt-3"{% if mag_scope == 'custom' %} open{% endif %}>
      <summary class="caps caps-soft cursor-pointer">limit to magazines{% if mag_scope == 'custom' %} <span class="ornament">·</span> {{ selected_titles|length }} of {{ all_titles|length }}{% endif %}</summary>
      <div class="mt-3">
        <div class="mb-3 flex gap-4 caps caps-soft">
          <button type="button" data-mag-all class="accent hover:underline">check all</button>
          <button type="button" data-mag-none class="accent hover:underline">check none</button>
        </div>
        <div class="mag-grid">
          {% for t in all_titles %}
          <label class="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" name="mag" value="{{ t }}"{% if t in selected_titles %} checked{% endif %}
                   class="accent-[var(--accent)]">
            <span class="italic font-serif">{{ t }}</span>
          </label>
          {% endfor %}
        </div>
      </div>
    </details>
```

- [ ] **Step 4: Run the panel tests + full web-search suite**

Run: `python -m pytest tests/test_web_search.py -v`
Expected: PASS — the 3 new panel/persistence tests plus all existing tests.

- [ ] **Step 5: Commit**

```bash
git add src/magsearch/web/templates/search.html src/magsearch/web/templates/base.html tests/test_web_search.py
git commit -m "feat: magazine-scope panel and link threading on search page"
```

---

### Task 4: Client JS — check-all / check-none and submit normalization

The template renders correctly and the server contract is fully tested via explicit params (Tasks 2–3). This task adds the browser behavior that produces those params from the form. It is verified manually with a headless browser (JS is not unit-tested in this repo).

**Files:**
- Modify: `src/magsearch/web/templates/base.html` (the existing `<script>` block near the end)

**Interfaces:**
- Consumes: the panel markup from Task 3 (`input[name="mag"]`, `input[name="mag_scope"]`, `[data-mag-all]`, `[data-mag-none]`).
- Produces: no server interface; on submit the form emits `mag_scope=custom` + checked `mag=` params when narrowed, and nothing (clean URL) when all checked.

- [ ] **Step 1: Add the JS**

In `src/magsearch/web/templates/base.html`, inside the existing `<script>` block, add before its closing `</script>`:

```javascript
  // Magazine-scope panel: check-all / check-none buttons.
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (!t.matches || !t.matches("[data-mag-all], [data-mag-none]")) return;
    var form = t.closest("form");
    if (!form) return;
    var on = t.matches("[data-mag-all]");
    form
      .querySelectorAll('input[type="checkbox"][name="mag"]')
      .forEach(function (b) { b.checked = on; });
    e.preventDefault();
  });

  // Magazine-scope: normalize the selection on submit.
  //   all checked  -> disable mag_scope + all mag boxes  (clean default URL)
  //   otherwise    -> mag_scope=custom; only checked mag boxes serialize
  //                   (none checked -> just mag_scope=custom -> no results)
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || form.tagName !== "FORM") return;
    var boxes = form.querySelectorAll('input[type="checkbox"][name="mag"]');
    if (!boxes.length) return;
    var scope = form.querySelector('input[type="hidden"][name="mag_scope"]');
    var allChecked = true;
    boxes.forEach(function (b) { if (!b.checked) allChecked = false; });
    if (allChecked) {
      if (scope) scope.disabled = true;
      boxes.forEach(function (b) { b.disabled = true; });
    } else if (scope) {
      scope.value = "custom";
    }
  }, true);
```

- [ ] **Step 2: Manual verification (headless browser)**

Start the app against a corpus (auth off) and drive the search form. Recommended (uses the same throwaway-DB approach as before so the user's DB is untouched):

```bash
cp data/magsearch.local.db .scratch/preview.db
MAGSEARCH_DATABASE_URL="sqlite:///./.scratch/preview.db" .venv/bin/alembic upgrade head
MAGSEARCH_AUTH_ENABLED=false MAGSEARCH_DATABASE_URL="sqlite:///./.scratch/preview.db" \
  .venv/bin/python -m uvicorn --factory magsearch.web.app:create_app --host 127.0.0.1 --port 8079 &
```

Then confirm, with a headless Chromium screenshot or by inspecting the emitted URL:
1. Open `/search?q=amiga`, expand "limit to magazines" → all titles listed and checked.
2. Uncheck one title, submit → URL contains `mag_scope=custom` and `mag=` only for the still-checked titles; results narrow accordingly.
3. Re-check all (or "check all"), submit → URL has no `mag_scope`/`mag` params (clean default); all results return.
4. "check none" then submit → `mag_scope=custom` with no `mag`; zero results.

Record the observed URLs / a screenshot in the task report. Stop the server and remove `.scratch/preview.db` afterward.

- [ ] **Step 3: Commit**

```bash
git add src/magsearch/web/templates/base.html
git commit -m "feat: check-all/none and submit normalization for magazine scope"
```

---

## Final verification

- [ ] **Run the whole suite**

Run: `python -m pytest -q`
Expected: all pass except the two pre-existing, unrelated `tests/test_ocr.py` CUDA-fault failures (baseline). No new failures, no new skips.

- [ ] **Manual smoke of the full feature** (see Task 4 Step 2) — confirm the panel opens on demand, buttons work, selection narrows results, and the selection survives a view/sort toggle.

## Self-review notes

- **Spec coverage:** by-title panel (Task 3), all-checked default + clean URL (Task 1 no-filter path + Task 4 submit strip), subset restricts (Task 1/2), none → no results (Task 1 `[]` short-circuit + Task 2 empty selection + Task 4 none-submit), scope marker encoding (Task 2 parse + Task 3 macro), expanding-bindparam IN (Task 1), persistence across links (Task 3 macro + `test_search_scope_persists_across_view_toggle`), on-demand disclosure + check-all/none (Task 3 `<details>` + Task 4 buttons), `/search`-only scope (Tasks leave per-issue/per-title untouched) — all covered.
- **Type consistency:** `titles: list[str] | None` in `search`/`search_magazines`; route builds `search_titles: list[str] | None` and `selected_titles`/`all_titles: list[str]`, passing `selected_titles` to the template as a `set`. `mag_scope` is `str` end to end; `mag` is `list[str]` at the route.
- **No placeholders:** every code and test step contains complete content.
