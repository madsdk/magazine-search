# Sort grouped search results by match count — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `matches` sort option to the grouped `/search` view that orders issues by descending number of matching pages, tie-broken by `best_rank`.

**Architecture:** A new `GROUPED_SORT_OPTIONS` whitelist sits alongside the existing `FLAT_SORT_OPTIONS` in `web/search.py`. The grouped SQL builder gains one extra ORDER BY clause (`COUNT(*) DESC, best_rank`). The search route picks the right whitelist based on `view`. The template loop renders the new option automatically.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy Core (`text()`), SQLite FTS5, Jinja2, pytest.

Reference spec: [`docs/superpowers/specs/2026-06-03-search-sort-by-matches-design.md`](../specs/2026-06-03-search-sort-by-matches-design.md).

---

### Task 1: Add `matches` sort to the grouped search SQL layer (TDD)

**Files:**
- Modify: `src/magsearch/web/search.py` (add `GROUPED_SORT_OPTIONS` constant; add `"matches"` key to `_GROUPED_ORDER_CLAUSES`)
- Test: `tests/test_search.py` (append new test using the existing `two_magazines_db` fixture)

The `two_magazines_db` fixture already in `tests/test_search.py` produces:
- `byte-1985-12` — 2 matching pages for "synthesizer", published 1985-12-01
- `compute-1984-06` — 1 matching page for "synthesizer", published 1984-06-01

That's enough to assert that `sort="matches"` orders by descending match count regardless of publication date (with `sort="newest"` Byte would also come first, but with `sort="oldest"` Compute would — so a separate `sort="oldest"` check below proves the new sort is doing the work, not a date-based artifact).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_search.py` (after `test_search_magazines_sort_newest`):

```python
def test_search_magazines_sort_matches_orders_by_count(two_magazines_db):
    # Byte has 2 matching pages, Compute has 1. With sort="matches", Byte
    # must come first regardless of publication date.
    factory = two_magazines_db
    with session_scope(factory) as s:
        groups = search_magazines(
            s, "synthesizer", offset=0, limit=10, sort="matches",
        )
    assert [g.magazine_id for g in groups] == ["byte-1985-12", "compute-1984-06"]
    assert [g.match_count for g in groups] == [2, 1]

    # Sanity check that the existing oldest sort still puts Compute first —
    # this confirms the new sort is ordering by count, not by date.
    with session_scope(factory) as s:
        oldest = search_magazines(
            s, "synthesizer", offset=0, limit=10, sort="oldest",
        )
    assert oldest[0].magazine_id == "compute-1984-06"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_search.py::test_search_magazines_sort_matches_orders_by_count -v
```

Expected: FAIL. The `sort="matches"` argument falls through `_pick(_GROUPED_SQL_BY_SORT, "matches", "rank")` to the `rank` clause, which orders by `best_rank` first — Compute happens to have a single very-clean match and might land first. Either way, the assertion `[g.match_count for g in groups] == [2, 1]` will not hold deterministically without the new clause. (If by luck the assertion does pass with the `rank` fallback, the test still fails on a later step — keep going.)

- [ ] **Step 3: Add the new sort key to `_GROUPED_ORDER_CLAUSES`**

In `src/magsearch/web/search.py`, edit the `_GROUPED_ORDER_CLAUSES` dict (defined just below `_PER_ISSUE_ORDER_CLAUSES`):

```python
_GROUPED_ORDER_CLAUSES = {
    "rank":    "best_rank, MIN(magazines.publication_date) ASC, magazines.title",
    "newest":  "MAX(magazines.publication_date) DESC, best_rank",
    "oldest":  "MIN(magazines.publication_date) ASC, best_rank",
    "matches": "COUNT(*) DESC, best_rank",
}
```

Notes:
- `best_rank` is already aliased in the grouped `SELECT` (`MIN(pages_fts.rank) AS best_rank`), so the ORDER BY can reference it directly.
- `_GROUPED_SQL_BY_SORT = _build_grouped_sql()` is built by iterating this dict, so the new key gains a compiled `text()` statement with no further code changes.

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/test_search.py::test_search_magazines_sort_matches_orders_by_count -v
```

Expected: PASS.

- [ ] **Step 5: Add the `GROUPED_SORT_OPTIONS` constant**

In `src/magsearch/web/search.py`, find the existing block:

```python
FLAT_SORT_OPTIONS = ("rank", "newest", "oldest")
PER_ISSUE_SORT_OPTIONS = ("rank", "page")
DEFAULT_FLAT_SORT = "rank"
DEFAULT_PER_ISSUE_SORT = "rank"
```

Add a new constant right below `FLAT_SORT_OPTIONS`:

```python
FLAT_SORT_OPTIONS = ("rank", "newest", "oldest")
GROUPED_SORT_OPTIONS = ("rank", "newest", "oldest", "matches")
PER_ISSUE_SORT_OPTIONS = ("rank", "page")
DEFAULT_FLAT_SORT = "rank"
DEFAULT_PER_ISSUE_SORT = "rank"
```

No new default constant is needed — `"rank"` is valid in both whitelists, so `DEFAULT_FLAT_SORT` is reused below.

- [ ] **Step 6: Run the full search test module to confirm nothing regressed**

```bash
pytest tests/test_search.py -v
```

Expected: all existing tests still pass plus the new one.

- [ ] **Step 7: Commit**

```bash
git add src/magsearch/web/search.py tests/test_search.py
git commit -m "Sort grouped search results by match count"
```

---

### Task 2: Add the tie-break test for equal match counts (TDD)

**Files:**
- Test: `tests/test_search.py` (new fixture + new test)

Goal: prove that when two issues have the same `match_count` but different `best_rank` values, `sort="matches"` returns the issue with the better (smaller) `best_rank` first.

FTS5's default `rank` (bm25) gives a smaller (better) value when the matched term takes up a larger share of the document. So an issue whose matching page contains only the search term will rank better than one whose matching page contains the term buried among many unrelated words.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_search.py`. The fixture below builds two magazines that each have exactly one matching page for "synthesizer", but on one magazine the page contains only the term, and on the other the term is buried in long noisy text — producing a deterministic best_rank difference:

```python
@pytest.fixture
def tied_match_count_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    bundles = tmp_path / "bundles"

    # "Crisp" — one matching page, term in isolation → better (smaller) bm25 rank.
    crisp = IngestPipeline(
        bundles, FakeOCREngine(responses=[
            [OCRRegion(text="synthesizer", bbox=(0,0,50,10), confidence=1.0)],
        ]),
        IngestOptions(title="Crisp", publication_date=date(1985, 1, 1)),
    ).run(make_pdf(tmp_path / "crisp.pdf", num_pages=1))

    # "Noisy" — one matching page, term buried in many unrelated words → worse rank.
    noisy_text = (
        "apple commodore atari amiga modem floppy disk drive printer "
        "keyboard mouse joystick monitor cable cartridge cassette tape "
        "synthesizer review continues with lengthy unrelated commentary "
        "about hardware peripherals and software titles of the era"
    )
    noisy = IngestPipeline(
        bundles, FakeOCREngine(responses=[
            [OCRRegion(text=noisy_text, bbox=(0,0,500,10), confidence=1.0)],
        ]),
        IngestOptions(title="Noisy", publication_date=date(1986, 1, 1)),
    ).run(make_pdf(tmp_path / "noisy.pdf", num_pages=1))

    with session_scope(factory) as s:
        import_bundle(crisp.bundle_dir, s)
        import_bundle(noisy.bundle_dir, s)
    return factory


def test_search_magazines_sort_matches_tiebreak_by_rank(tied_match_count_db):
    # Both issues have match_count == 1. With sort="matches", the better-ranked
    # issue (Crisp — search term in isolation) must come first.
    factory = tied_match_count_db
    with session_scope(factory) as s:
        groups = search_magazines(
            s, "synthesizer", offset=0, limit=10, sort="matches",
        )
    assert len(groups) == 2
    assert all(g.match_count == 1 for g in groups)
    assert groups[0].magazine_id == "crisp-1985-01"
    assert groups[1].magazine_id == "noisy-1986-01"
```

- [ ] **Step 2: Run test to verify it passes**

```bash
pytest tests/test_search.py::test_search_magazines_sort_matches_tiebreak_by_rank -v
```

Expected: PASS. The ORDER BY from Task 1 (`COUNT(*) DESC, best_rank`) already covers this case — this test simply locks in the tie-break behaviour against future regression.

Note: this is a verification test rather than a TDD-fail-first test. The SQL change that enables it was made in Task 1; if it fails here, the fixture is wrong, not the production code. If the test fails, inspect `best_rank` values in the result and adjust the noisy text length until the bm25 gap is wide enough to be deterministic across SQLite versions.

- [ ] **Step 3: Commit**

```bash
git add tests/test_search.py
git commit -m "Test tie-break by best_rank for matches sort"
```

---

### Task 3: Wire `GROUPED_SORT_OPTIONS` into the search route (TDD)

**Files:**
- Modify: `src/magsearch/web/routes.py` (import the new constant; pick whitelist based on view)
- Test: `tests/test_web_search.py` (existing file; append new tests)

The fixture used by every test in `tests/test_web_search.py` is `app_client` (defined in `tests/conftest.py`). It returns `(client, bundles)` and pre-seeds two magazines:

- `byte-1985-12` — 2 pages: `"vintage synthesizer review"` and `"modem speeds compared"`
- `compute-1984-06` — 1 page: `"apple ii basic"`

No single bare query matches both issues. To exercise differing match counts across the two issues, use the OR query `"synthesizer modem apple"` with `match_all=0`:

- Byte: 2 matching pages (page 1 = "synthesizer", page 2 = "modem")
- Compute: 1 matching page (page 1 = "apple")

This is the same pattern the existing `test_search_match_all_off_uses_or` (line 201 of `test_web_search.py`) already relies on.

- [ ] **Step 1: Write the failing tests**

Append the following three tests to the bottom of `tests/test_web_search.py`:

```python
def test_search_route_grouped_offers_matches_sort(app_client):
    # The grouped view's sort row should include a sort=matches link.
    client, _ = app_client
    resp = client.get(
        "/search",
        params={"q": "synthesizer modem apple", "view": "grouped",
                "match_all": 0},
    )
    assert resp.status_code == 200
    assert "sort=matches" in resp.text


def test_search_route_flat_omits_matches_sort(app_client):
    # The flat view's sort row must NOT include a sort=matches link.
    client, _ = app_client
    resp = client.get(
        "/search",
        params={"q": "synthesizer modem apple", "view": "flat",
                "match_all": 0},
    )
    assert resp.status_code == 200
    assert "sort=matches" not in resp.text


def test_search_route_grouped_accepts_matches_sort(app_client):
    # sort=matches in the grouped view must be honoured (no fallback to
    # "rank"). Byte (2 matching pages) must appear before Compute (1).
    client, _ = app_client
    resp = client.get(
        "/search",
        params={"q": "synthesizer modem apple", "view": "grouped",
                "match_all": 0, "sort": "matches"},
    )
    assert resp.status_code == 200
    byte_pos = resp.text.find("byte-1985-12")
    compute_pos = resp.text.find("compute-1984-06")
    assert byte_pos != -1 and compute_pos != -1, resp.text
    assert byte_pos < compute_pos


def test_search_route_flat_rejects_matches_sort(app_client):
    # sort=matches is not valid in the flat view; _validate_sort must fall
    # back to the default. The response should still render 200 and the
    # rendered sort control must not show "matches" as the active choice.
    client, _ = app_client
    resp = client.get(
        "/search",
        params={"q": "synthesizer modem apple", "view": "flat",
                "match_all": 0, "sort": "matches"},
    )
    assert resp.status_code == 200
    # The template renders the active sort as a bare <span>{{ s }}</span>
    # and inactive options as anchor tags. A literal "<span>matches</span>"
    # would mean the bogus sort survived validation.
    assert "<span>matches</span>" not in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_web_search.py -v -k matches_sort
```

Expected: FAIL. The route currently passes `FLAT_SORT_OPTIONS` to the template for both views, so the grouped view does not render a `sort=matches` link, and `_validate_sort("matches", FLAT_SORT_OPTIONS, "rank")` rejects it even in grouped mode.

- [ ] **Step 3: Import the new constant in routes**

In `src/magsearch/web/routes.py`, update the import block from `magsearch.web.search`:

```python
from magsearch.web.search import (
    DEFAULT_FLAT_SORT,
    DEFAULT_PER_ISSUE_SORT,
    FLAT_SORT_OPTIONS,
    GROUPED_SORT_OPTIONS,
    PER_ISSUE_SORT_OPTIONS,
    search,
    search_in_magazine,
    search_in_magazine_title,
    search_magazines,
)
```

- [ ] **Step 4: Pick the whitelist based on view in `search_route`**

In `src/magsearch/web/routes.py`, locate `search_route` (the `@router.get("/search")` handler). Find this block:

```python
    if view not in ("grouped", "flat"):
        view = "grouped"
    sort = _validate_sort(sort, FLAT_SORT_OPTIONS, DEFAULT_FLAT_SORT)
```

Change it to:

```python
    if view not in ("grouped", "flat"):
        view = "grouped"
    sort_options = GROUPED_SORT_OPTIONS if view == "grouped" else FLAT_SORT_OPTIONS
    sort = _validate_sort(sort, sort_options, DEFAULT_FLAT_SORT)
```

Then, further down in the same function, find the template context:

```python
            "sort": sort,
            "per_page": per_page,
            "per_page_options": PER_PAGE_OPTIONS,
            "sort_options": FLAT_SORT_OPTIONS,
```

Change `"sort_options": FLAT_SORT_OPTIONS,` to `"sort_options": sort_options,`:

```python
            "sort": sort,
            "per_page": per_page,
            "per_page_options": PER_PAGE_OPTIONS,
            "sort_options": sort_options,
```

Do **not** touch the magazine-title route (`magazine_issues`) further down in the same file. It searches via `search_in_magazine_title`, which returns flat page results, not grouped results, so it correctly stays on `FLAT_SORT_OPTIONS`.

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_web_search.py -v -k matches_sort
```

Expected: PASS for all four new route tests.

- [ ] **Step 6: Run the full web-route + search test suite as a regression check**

```bash
pytest tests/test_search.py tests/test_web_search.py -v
```

Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/magsearch/web/routes.py tests/test_web_search.py
git commit -m "Expose matches sort in /search route for grouped view"
```

---

### Task 4: Manual smoke check

**Files:** none — interactive verification.

- [ ] **Step 1: Run the dev server against the test DB**

```bash
magsearch web --reload
```

(Or whatever the project's dev launch command is — see `README.md`.)

- [ ] **Step 2: Open `/search` and verify**

In a browser:

1. Issue a query that returns at least two issues with different match counts.
2. Confirm the sort row shows: `rank · newest · oldest · matches`.
3. Click `matches`. URL gains `&sort=matches`. The list re-orders so the issue with the largest "N matching pages" line comes first.
4. Toggle to flat view. The `matches` link is absent from the sort row. If the URL still has `sort=matches`, results are ordered by `rank` (default) — verify by reading the URL the dropdown emits.

- [ ] **Step 3: Report any UI surprises**

If the new option visually crowds the sort row at narrow widths, mention it in the PR description for follow-up — no template change is in scope for this task unless a real issue is observed.

- [ ] **Step 4: Final full-suite run**

```bash
pytest -v
```

Expected: 153 passed (the existing 151 plus the two new search-level tests), 1 skipped, 1 deselected — adjust counts for whatever route-level tests Task 3 added.

- [ ] **Step 5: Commit (only if any cleanup was made during smoke)**

If the smoke check produced no code changes, skip. Otherwise:

```bash
git add -p
git commit -m "<message describing the smoke-check fix>"
```

---

## Definition of Done

- `GROUPED_SORT_OPTIONS = ("rank", "newest", "oldest", "matches")` exists in `web/search.py`.
- `_GROUPED_ORDER_CLAUSES["matches"]` is `"COUNT(*) DESC, best_rank"`.
- `search_route` selects the whitelist by view and passes it to the template.
- New tests cover: descending count ordering, tie-break by best_rank, route-level rendering, and flat-view rejection of `sort=matches`.
- Manual smoke confirms the link appears in grouped view, is absent in flat view, and re-orders results.
- All existing tests still pass.
