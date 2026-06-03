# Sort grouped search results by match count

## Problem

The grouped `/search` view returns one row per issue with a `match_count`
("N matching pages") already computed. Users can sort by `rank`, `newest`, or
`oldest`, but not by the visible match count itself — so an issue with 30
matches and one with 1 match can sit side by side under "rank" with no easy
way to bring the issue that mentions the search term most often to the top.

## Goal

Add a `matches` sort option to the grouped `/search` view that orders issues
by descending number of matching pages, with `best_rank` (the FTS5 rank of
the best-matching page in the issue, ascending) as the tie-break.

Non-goals:

- No new sort option on the flat `/search` view.
- No new sort option on the magazine title page (`/magazines/<title>`) or the
  per-issue page (`/magazine/<id>`).
- No UI redesign — the new option appears in the existing sort dropdown via
  the template's `sort_options` loop.

## Design

### Sort whitelists

`src/magsearch/web/search.py` currently exposes a single
`FLAT_SORT_OPTIONS = ("rank", "newest", "oldest")` tuple that is reused for
both the flat and grouped views. Introduce a separate tuple for grouped:

```python
GROUPED_SORT_OPTIONS = ("rank", "newest", "oldest", "matches")
```

`FLAT_SORT_OPTIONS` keeps its current value. This mirrors how
`PER_ISSUE_SORT_OPTIONS` already lives alongside `FLAT_SORT_OPTIONS` for the
per-issue view.

`DEFAULT_FLAT_SORT` stays `"rank"`; it is a valid key in both whitelists, so
no second default constant is needed.

### ORDER BY clause

Add one entry to `_GROUPED_ORDER_CLAUSES`:

```python
"matches": "COUNT(*) DESC, best_rank",
```

- Primary key: `COUNT(*) DESC` — most matching pages first.
- Tie-break: `best_rank` (ascending; smaller FTS5 rank = better match) — the
  same primary key the existing `rank` clause uses, so equally-matched issues
  fall back to "the one whose best page matched best."

`best_rank` is already in the `SELECT` list of the grouped query
(`MIN(pages_fts.rank) AS best_rank`), so the new ORDER BY needs no further
SQL changes. `_GROUPED_SQL_BY_SORT` is built by iterating
`_GROUPED_ORDER_CLAUSES`, so the new key picks up a compiled `text()`
statement automatically.

`search_magazines()` looks the statement up via `_pick(_GROUPED_SQL_BY_SORT,
sort, DEFAULT_FLAT_SORT)`, so it requires no code change.

### Route wiring

In `src/magsearch/web/routes.py`, `search_route` currently validates against
`FLAT_SORT_OPTIONS` regardless of view:

```python
sort = _validate_sort(sort, FLAT_SORT_OPTIONS, DEFAULT_FLAT_SORT)
```

Change to pick the whitelist based on the active view:

```python
sort_options = GROUPED_SORT_OPTIONS if view == "grouped" else FLAT_SORT_OPTIONS
sort = _validate_sort(sort, sort_options, DEFAULT_FLAT_SORT)
```

…and pass the same `sort_options` into the template context (replacing the
current hard-coded `FLAT_SORT_OPTIONS`).

The `view` validation already runs immediately above, so by this point `view`
is guaranteed to be either `"grouped"` or `"flat"`.

### Template

`src/magsearch/web/templates/search.html` does not change. It already
iterates `sort_options` to render the dropdown, so the new `matches` link
appears for the grouped view and is absent for the flat view.

### URL/state behaviour

- A user who clicks `matches` while in the grouped view, then toggles to the
  flat view, will have `sort=matches` in the URL. `_validate_sort` against
  `FLAT_SORT_OPTIONS` rejects it and falls back to `DEFAULT_FLAT_SORT`
  ("rank"). This is the existing contract for unknown sort values.
- The default sort on a fresh `/search?q=…` request remains `rank`.

## Data flow

```
GET /search?q=…&view=grouped&sort=matches
        │
        ▼
routes.search_route
  ├─ view == "grouped"  → sort_options = GROUPED_SORT_OPTIONS
  ├─ _validate_sort("matches", GROUPED_SORT_OPTIONS, "rank")  → "matches"
  └─ search_magazines(..., sort="matches")
        │
        ▼
_pick(_GROUPED_SQL_BY_SORT, "matches", "rank")
        │
        ▼
SQLite: SELECT … ORDER BY COUNT(*) DESC, best_rank LIMIT … OFFSET …
        │
        ▼
search.html  iterates sort_options → "matches" link rendered active
```

## Testing

Add two tests to `tests/test_search.py`, alongside the existing
`test_search_magazines_sort_newest`:

1. **`test_search_magazines_sort_matches_orders_by_count`** — using the
   existing `two_magazines_db` fixture (Byte: 2 matches, Compute: 1 match for
   "synthesizer"), assert that `sort="matches"` puts Byte first regardless of
   publication date.

2. **`test_search_magazines_sort_matches_tiebreak_by_rank`** — a fixture (or
   an extension of an existing one) where two issues have the same
   `match_count` but different best-page FTS5 ranks. Assert ordering follows
   `best_rank` ascending. If composing this fixture is awkward, fall back to
   asserting via a direct comparison of `best_rank` between adjacent results
   rather than baked-in IDs.

No new tests are required for the route layer — the existing route tests
exercise `_validate_sort` via the `sort=` query parameter, and the
view-conditional whitelist is straightforward enough that the unit-level
tests above are sufficient.

## Out of scope

- Adding `matches` to the flat view (would require a different SQL shape and
  the user has not asked for it).
- Ascending-matches sort ("fewest matches first") — no use case identified.
- Persisting the user's preferred sort across sessions.
