# Limit search to selected magazines

## Summary

Add an on-demand panel to the main search page (`/search`) that lets a user
restrict the search to a chosen subset of magazine **titles**. The panel lists
every distinct title as a checkbox, all checked by default (search
everything). Unchecking narrows the search; a "check all" / "check none" pair
toggles the whole list. The panel is hidden until the user opens it.

## Motivation

Search matches text across the whole corpus. A user who knows the term they
want lives in a particular publication (e.g. "find *civilization* only in PC
Gamer and Amiga Power") has no way to scope the search. Per-issue search
already exists for a single issue; this fills the gap between "one issue" and
"the entire archive" by scoping to one or more titles.

## Semantics & URL encoding

- The panel lists **distinct magazine titles**, alphabetically. Each is a
  checkbox, **checked by default**.
- **Default — all checked:** no filter; search everything. The URL carries no
  magazine params (clean default).
- **Subset checked:** search is restricted to the checked titles.
- **None checked:** no results (an empty selection means "search nothing").
- **Scope marker.** To distinguish "default / all" from "explicitly none" in a
  plain GET URL, the form carries a scope marker:
  - Full set selected → URL carries neither `mag_scope` nor any `mag`
    param.
  - Narrowed → URL carries `mag_scope=custom` plus one `mag=<title>` per
    checked title, e.g. `…&mag_scope=custom&mag=PC+Gamer&mag=The+One`.
  - Server rule: `mag_scope == "custom"` → restrict to the listed `mag`
    titles (empty list → **no results**); any other value or absent → all
    (no filter).
- The marker + selected titles are threaded through **every** view / sort /
  per-page / pagination link, exactly as the year and match-mode params are.
- **Scope of the feature:** `/search` only — both grouped and flat views. The
  per-title (`/magazines/{title}`) and per-issue (`/magazine/{id}`) pages are
  unchanged.

### Interaction with existing filters

The magazine-title filter is ANDed with the existing full-text query and the
year-range filter. All three narrow the same result set independently.

## SQL

The common "all" path keeps using today's prebuilt `text()` statements
unchanged (including the year-range bind params added previously). When a title
subset is active, the statement is built on demand:

- Refactor the flat and grouped base SQL strings into shared module-level
  format-string constants so both the prebuilt (no-title) path and the
  dynamic (with-title) path use the same source. (Per-issue / per-title base
  SQL is not touched.)
- The dynamic statement appends `AND magazines.title IN :titles` to the base
  and binds `titles` via a SQLAlchemy **expanding** bind param
  (`bindparam("titles", expanding=True)`) — safe parameterized `IN`, never
  string interpolation of user values.
- **Empty custom selection short-circuits in Python:** if scope is custom and
  the selected-titles list is empty, `search()` / `search_magazines()` return
  `[]` immediately without touching the DB (avoids an invalid `IN ()` and
  correctly yields no results).

`search()` and `search_magazines()` gain a parameter:

```python
titles: list[str] | None = None,
```

- `titles is None` → no title filter (all). Uses the prebuilt statement.
- `titles == []` → custom-but-empty → return `[]`.
- `titles == [..]` → build dynamic statement, bind the list.

The per-issue and per-title functions (`search_in_magazine`,
`search_in_magazine_title`) are **not** modified.

## Route (`src/magsearch/web/routes.py`)

`search_route` gains:

- One extra query to populate the panel:
  `select(Magazine.title).distinct().order_by(Magazine.title)` → `all_titles:
  list[str]`.
- Parse the scope: `mag_scope: str = Query(default="")` and
  `mag: list[str] = Query(default=[])` (FastAPI collects repeated `mag`
  params into a list).
- Resolve the effective filter:
  - if `mag_scope == "custom"`: `selected = [t for t in mag if t in
    set(all_titles)]` (drop unknown titles defensively); pass
    `titles=selected` to the search call.
  - else: `selected = all_titles` (all checked in the UI); pass `titles=None`
    (no filter) to the search call.
- Pass to the template context: `all_titles`, `mag_scope` (normalized to
  `"custom"` or `""`), and `selected_titles` (a set for fast `in` checks in
  the template).

Accept `mag_scope` / `mag` as strings/list so junk never triggers a 422;
unknown titles are silently dropped. Consistent with the "never raise on bad
input" rule.

## Template (`src/magsearch/web/templates/search.html`)

- **Disclosure:** a `<details>` element in the options row. `<summary>` reads
  **"limit to magazines"**; when a custom subset is active it also shows a
  count, e.g. "limit to magazines · 3 of 12". The element is rendered `open`
  when `mag_scope == "custom"` so the selection stays visible after a search;
  collapsed otherwise.
- **Panel body:** a **[check all]** / **[check none]** button pair
  (`type="button"`), then a scrollable, responsive grid of checkboxes — one
  per title in `all_titles`, `name="mag"`, `value="<title>"`,
  `checked` when the title is in `selected_titles`. A max-height with
  `overflow-y:auto` keeps a large list manageable.
- **Scope marker input:** a hidden `<input name="mag_scope">` (default value
  `all`) that the submit script sets to `all` / `custom`.
- **Link threading:** the `search_url` macro appends `mag_scope=custom` and one
  `mag=<title>` per selected title when `mag_scope == "custom"`; nothing when
  all. Titles are URL-encoded (`|urlencode`).

## Client JS (`src/magsearch/web/templates/base.html`)

Add to the existing `<script>` block (which already normalizes checkbox/hidden
twins and match-mode locking):

- **check all / check none:** click handlers on the two buttons set every
  `input[name="mag"]` checkbox in the panel checked / unchecked.
- **Submit normalization:** on the search form's submit, count checked vs total
  `mag` boxes:
  - all checked → set `mag_scope` value to `all` and disable it + all `mag`
    boxes so they don't serialize (clean default URL).
  - otherwise → set `mag_scope` to `custom`; only the checked `mag` boxes
    serialize (an all-unchecked panel serializes just `mag_scope=custom`,
    yielding no results per the server rule).

The disclosure open/close itself needs no JS (`<details>` handles it); the
count in the summary is server-rendered from `selected_titles`.

## Testing

Search-layer unit tests (extend `tests/test_search.py`, reusing a
multi-title fixture):

- `titles=None` → unchanged, searches all titles.
- `titles=["Byte"]` → only that title's pages.
- `titles=["Byte", "Compute"]` → both, others excluded.
- `titles=[]` → `[]` (no DB hit; empty custom selection).
- grouped variant (`search_magazines`) restricts the same way.
- title filter combines with a year bound (AND semantics).

Route / template tests (extend `tests/test_web_search.py`):

- The panel lists all corpus titles, each checked, by default; no `mag` param
  in the default form.
- `mag_scope=custom&mag=Byte` narrows results to that title and round-trips:
  the form renders with only that box checked and `<details open>`.
- `mag_scope=custom` with no `mag` → no results.
- Bad input (`mag_scope=custom&mag=Nonexistent`) → no 500; unknown title
  dropped → no results (empty effective selection).
- The magazine selection persists across a view toggle (grouped ↔ flat): the
  toggle link carries `mag_scope=custom` and the `mag=` params.

## Out of scope

- Per-issue granularity in the panel (titles only).
- Filtering on the per-title / per-issue search pages.
- Search-within-the-panel, "recently used", or saved magazine sets.
- Any change to ingestion, the data model, or how titles are stored.
