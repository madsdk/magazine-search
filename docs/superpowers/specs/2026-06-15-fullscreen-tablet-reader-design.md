# Fullscreen Tablet Reader — Design

**Date:** 2026-06-15
**Status:** Approved, ready for implementation planning

A fullscreen, touch-first reading mode for browsing magazine scans on a tablet.
A reader opens an entire page letterboxed to fit the viewport and flips between
pages with left/right swipe gestures using a sliding-carousel animation. It is
launched from a button on the existing page viewer and is a web-only feature.

## Goals

- Make reading a magazine on a tablet feel like a native reader: one full page
  at a time, flipped by swiping left/right with a tactile sliding animation.
- Keep the entry frictionless — one button on the existing page viewer opens
  the reader at the current page.
- Stay within the project's shape: FastAPI + Jinja2 routes, small well-bounded
  units, server tests in the existing pytest style.

## Non-Goals (v1)

- Two-page spreads in landscape. The reader always shows a single page.
- Jump-to-page input or a thumbnail scrubber inside the reader.
- Search-term highlighting inside the reader. `?q=` is preserved only so that
  Exit returns to the existing (highlightable) page viewer. Porting the
  SVG-overlay highlight into the reader can come later.
- Authenticating the `/bundle/{path}` image route. It is not auth-gated today;
  changing it would affect the existing viewer and is out of scope.
- Supporting the standalone desktop app (QtWebEngine 5.15 / Chromium 87). This
  is a web-only feature, so modern touch/pointer events and modern CSS are
  fair game.

## Decisions Made During Brainstorming

| # | Decision | Rationale |
|---|---|---|
| 1 | Web-only feature | User confirmed the desktop app is out of scope, removing the Chromium 87 constraint |
| 2 | Entry via a button on the existing page viewer | Frictionless; leaves browse/search/magazine flows unchanged |
| 3 | Always a single page (portrait and landscape) | Simplest, consistent; each swipe = one page; avoids odd/even pairing logic |
| 4 | Controls: auto-hiding Exit + page indicator, pinch-to-zoom, tap zones to flip | The essential reading affordances; jump/thumbnails deferred |
| 5 | Sliding-carousel flip that tracks the finger | Smoothest, most app-like; chosen over instant swap |
| 6 | Dedicated reader route + JSON page-list endpoint (Approach A) | Keeps the already-large `page.html` clean; reader is an isolated, testable unit; decouples from image-path naming |
| 7 | New endpoints require authentication | User requirement; reuses the existing `require_user` dependency |
| 8 | In-reader highlighting deferred | Keeps v1 focused; `?q=` round-trips so the viewer's highlight still works on Exit |

## Architecture

Approach A — a dedicated reader route backed by a small JSON page-list endpoint,
with all reader behavior in a standalone static JS module. Three new units:

### Route: `GET /magazine/{id}/read/{page}`

- Dependency: `user: User = Depends(require_user)` — unauthenticated web users
  get the standard `303 → /login?next=…` redirect.
- Validates the magazine exists (404 otherwise) and clamps `page` into
  `1..page_count`.
- Accepts `?q=` and passes it through to the template so highlighting state
  survives entering the reader (used only by the Exit link in v1).
- Renders a new standalone template `reader.html` that does **not** extend
  `base.html` — it is a minimal full-viewport document (no masthead/footer).
  The template seeds the JS with the magazine id, starting page, page count,
  and `q`.

### Endpoint: `GET /magazine/{id}/pages.json`

- Dependency: `user: User = Depends(require_user)` (same redirect behavior).
- Validates the magazine exists (404 otherwise).
- Returns, read from the DB (authoritative — not pattern-guessed from page
  numbers):

  ```json
  {
    "page_count": 80,
    "pages": [
      { "n": 1, "image_path": "ace-1987-10/pages/0001.webp" },
      { "n": 2, "image_path": "ace-1987-10/pages/0002.webp" }
    ]
  }
  ```

- Ordered by `page_number` ascending. The reader JS uses this to build the
  carousel and preload neighbors. Image bytes continue to be served by the
  existing `GET /bundle/{path}` route.

### Static module: `static/reader.js`

All reader behavior lives here, kept out of inline template script so it is
isolated and lint-able. Loaded by `reader.html`.

### Entry point: button on `page.html`

`page.html` gains a single **"Read fullscreen"** icon-button in the existing
control row, linking to `/magazine/{id}/read/{page}?q=…` for the current page.
This is the only change to `page.html`.

## Reader UI & Behavior

- Full-viewport dark backdrop. The current page image is letterboxed with
  `object-fit: contain` so an entire page is always visible regardless of
  orientation.
- **Auto-hiding overlay bar:** an Exit (✕) button and a `p. 12 / 80` page
  indicator. Appears on tap; fades after ~3s of inactivity. Exit returns to the
  page viewer (`/magazine/{id}/page/{page}?q=…`) for the current page, where
  zoom/rotate/highlight still work.
- **Swipe** left/right flips pages via the sliding carousel (see below).
- **Tap zones:** tapping the left ~25% of the screen flips back, the right ~25%
  flips forward, and the center toggles the overlay bar.
- **Pinch-to-zoom and double-tap-to-zoom** on the current page, with drag-to-pan
  when zoomed.
- **Gesture arbitration:** when zoomed in (zoom > 1), a one-finger drag pans the
  page and swipe-to-flip is suspended; at zoom = 1, a one-finger drag swipes to
  flip. Returning to zoom = 1 re-enables flipping. Zoom resets to 1 when a flip
  settles on a new page.
- **Keyboard:** ←/→ flip and Esc exits (harmless and cheap for tablets with
  keyboards).

## Carousel & Preloading

- The DOM holds a 3-slide track — **prev / current / next** — laid out side by
  side and translated horizontally. Finger drag updates the translate in real
  time.
- On release, snap to the neighbor if the drag passed a distance **or** velocity
  threshold; otherwise spring back to the current page.
- After a flip settles, the track recenters on the new current page and the new
  outgoing neighbor's `<img>` is pointed at the correct `image_path`, so there
  is always one page of preload buffer in each direction.
- The first page has no prev slide and the last page has no next slide — the
  track cannot be over-swiped past either end.
- Entering the reader preloads the current page image immediately to minimize
  the blank-flash from the navigation.

## Error Handling

- Unknown magazine id → 404 from both new endpoints.
- Out-of-range `page` in the reader route → clamped into `1..page_count`.
- Unauthenticated request (web) → `303 → /login?next=…` via `require_user`.
- A failed image load in the carousel shows a quiet placeholder rather than a
  broken-image icon; flipping past it still works.

## Testing

### Server (pytest, existing style)

- `pages.json` returns the correct shape and ascending `page_number` ordering.
- `pages.json` 404s for an unknown magazine.
- The reader route renders 200 for a valid magazine/page and clamps an
  out-of-range page into `1..page_count`.
- Both new endpoints `303`-redirect to `/login` when unauthenticated and return
  `200` when authenticated.

### Client

- Factor the swipe-decision (distance/velocity → flip vs. spring-back) and the
  zoom/pan-vs-swipe arbitration into small pure functions so they can be unit
  tested without a browser, if the project has a JS test path. The exact JS test
  setup (if any) will be confirmed during planning; absent one, these functions
  stay pure and documented.

## Open Questions for Planning

- Whether any JS test harness exists in the repo, or the client logic ships as
  pure-but-untested functions for v1.
