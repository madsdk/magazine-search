# Year-range filtering for search

## Summary

Add an optional publication-year filter to the main search page (`/search`).
Users can restrict results to magazines published within a year range by
setting a **From year**, a **To year**, both, or neither. The filter works
alongside the existing full-text query, view toggle (grouped/flat), sort, and
pagination.

## Motivation

Search today matches text across the whole corpus with no way to scope results
to a period. For a collection of old magazines spanning decades, "find *pacman*
in issues from 1980–1983" is a natural request that isn't currently possible.
The corpus already records `publication_date` per magazine; this feature
exposes it as a filter.

## Behavior & semantics

Two optional inputs on the search form: **From year** and **To year**. Either,
both, or neither may be set.

- A set **From year** `Y` means `publication_date >= Y-01-01`.
- A set **To year** `Y` means `publication_date <= Y-12-31`.
- **Undated issues** (`publication_date IS NULL`) are **excluded** whenever
  *either* bound is set, and appear normally when *neither* is set. This is a
  consequence of SQL three-valued logic: `NULL >= '1980-01-01'` evaluates to
  `NULL` (falsy), so undated rows drop out on their own — no explicit
  `IS NOT NULL` guard needed.
- **Scope:** the filter applies to `/search` only — both the grouped and flat
  views. The per-title page (`/magazines/{title}`) and single-issue search
  (`/magazine/{id}`) are unchanged.

### Input robustness

Years arrive from free-form inputs and from URL query params, so both are
treated as untrusted. The rules, applied by a single coercion helper:

- Non-numeric or empty → that bound is treated as blank (unbounded).
- Out-of-range years → treated as blank. Accept a plausible range of
  `1000`–`9999`; anything outside is blank. (This is a sanity clamp against
  junk like `year_from=0` or `year_from=20260`, not a corpus-specific limit.)
- If both bounds are set and `from > to`, **swap** them.
- Search never raises on bad year input — consistent with today's FTS
  behavior, where an unparseable query returns `[]` rather than erroring.

## Implementation

### Search layer (`src/magsearch/web/search.py`)

The four search SQL statements are pre-built once as static `text()` objects
keyed by sort. To avoid a combinatorial explosion of WHERE-clause variants
(none / from-only / to-only / both), add **two always-present bind params** to
the **flat** and **grouped** builders only:

```sql
... WHERE pages_fts MATCH :q
    AND (:date_from IS NULL OR magazines.publication_date >= :date_from)
    AND (:date_to   IS NULL OR magazines.publication_date <= :date_to)
```

- When a param binds to `NULL`, its clause is a no-op (matches everything).
- When a param is set, the `NULL`-comparison rule handles undated-exclusion
  for free, as described above.
- `publication_date` is stored by SQLite as ISO `YYYY-MM-DD` text, so
  lexicographic string comparison against the boundary strings is equivalent
  to chronological comparison. Boundaries are `f"{year}-01-01"` (from) and
  `f"{year}-12-31"` (to).

`search()` (flat) and `search_magazines()` (grouped) each gain:

```python
year_from: int | None = None,
year_to:   int | None = None,
```

They convert the years to ISO boundary strings (or `None`) and add
`date_from` / `date_to` to the bound-params dict.

The **per-issue** (`_build_per_issue_sql`) and **per-title**
(`_build_per_title_sql`) builders and their functions
(`search_in_magazine`, `search_in_magazine_title`) are left unchanged — they
are out of scope. Their `text()` objects will not carry the new bind params,
so the two shared clause strings must not be applied to them blindly.

### Coercion helper

A small module-level helper turns raw inputs into a validated
`(year_from, year_to)` pair:

```python
def coerce_year_range(
    raw_from: object, raw_to: object
) -> tuple[int | None, int | None]:
    """Parse/validate two year inputs. Non-numeric or out-of-range → None.
    If both set and from > to, swap. Never raises."""
```

Placed in `search.py` alongside `sanitize_query`. Keeping it here (rather than
in the route) keeps the route thin and lets it be unit-tested directly.

### Web layer (`src/magsearch/web/routes.py`)

`search_route` gains two query params:

```python
year_from: str = Query(default=""),
year_to:   str = Query(default=""),
```

(Accept as `str` so junk never triggers FastAPI's own 422 — coercion is our
job.) The route calls `coerce_year_range`, passes the resulting ints to
`search(...)` / `search_magazines(...)`, and puts the **coerced** values into
the template context (so the form reflects what was actually applied, e.g. a
swapped range).

### Template (`src/magsearch/web/templates/search.html`)

- Two `<input type="number">` fields ("From year" / "To year") in the search
  form, pre-filled from the coerced context values.
- The `search_url` macro and the hidden form inputs must thread
  `year_from` / `year_to` through **every** view / sort / per-page /
  pagination link — exactly as `match_all` / `match_phrase` are threaded
  today — so toggling a view or paging does not drop the active date filter.
- Blank bounds render as empty inputs and are omitted from (or sent empty in)
  the generated URLs.

## Testing (TDD)

Unit tests on the search functions (against a small in-memory / temp fixture
corpus with a mix of dated and undated issues):

- from-only bound filters correctly
- to-only bound filters correctly
- both bounds (closed range) filter correctly
- undated issue **excluded** when either bound is set
- undated issue **included** when neither bound is set
- reversed years (`from > to`) are swapped and return the same as the ordered
  range
- junk input (non-numeric, out-of-range) coerced to blank → behaves as no
  filter

Unit tests on `coerce_year_range` directly for each rule (blank, junk, clamp,
swap).

Route-level test:

- `year_from` / `year_to` round-trip into the rendered form
- the active date filter persists across a view toggle (grouped ↔ flat) and
  across a sort change

## Out of scope

- Date filtering on per-title and single-issue search (possible fast-follow).
- Full-date (month/day) precision — year granularity only.
- A slider / min–max-of-corpus UI affordance; plain number inputs only.
- Any change to how `publication_date` is ingested or stored.
