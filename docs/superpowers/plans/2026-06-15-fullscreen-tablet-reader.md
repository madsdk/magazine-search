# Fullscreen Tablet Reader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a web-only, touch-first fullscreen reading mode that shows one whole magazine page at a time and flips pages with a left/right swipe carousel, launched from a button on the existing page viewer.

**Architecture:** A dedicated authenticated reader route (`/magazine/{id}/read/{page}`) renders a minimal full-viewport template that does not extend `base.html`. A second authenticated JSON endpoint (`/magazine/{id}/pages.json`) returns the page list (number + image path) from the database. All reader behavior — sliding carousel, swipe/tap-zone flipping, pinch-zoom with gesture arbitration, and the auto-hiding overlay — lives in a single static module `reader.js`. Page image bytes continue to be served by the existing `/bundle/{path}` route.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, SQLAlchemy (SQLite), pytest (server tests). Vanilla browser JS for the reader (no build step, no JS test harness in this repo).

---

## Background for the implementer

You are working in `magsearch`, a self-hosted full-text search app for scanned
magazines. Relevant facts:

- The web layer is FastAPI + Jinja2. Content routes live in
  `src/magsearch/web/routes.py` on an `APIRouter` named `router`, with templates
  loaded via `_TEMPLATES = Jinja2Templates(directory=.../templates)`.
- The existing single-page viewer is the route
  `GET /magazine/{magazine_id}/page/{page_number}` (see `routes.py:278`) which
  renders `templates/page.html`.
- Auth: there is an `AuthMiddleware`. A global `require_login` config flag
  (default **off**) can force login for all non-allow-listed routes, but by
  default anonymous users can browse. To require login on a *specific* endpoint
  regardless of that flag, add the dependency `Depends(require_user)` from
  `magsearch.web.deps`. `require_user` raises `HTTPException(status_code=303, …)`
  with a `Location: /login?next=…` header when there is no current user, and
  resolves successfully when a user is logged in (or in desktop/auth-off mode,
  where a synthesized local admin is pinned — not relevant to this web-only
  feature but harmless).
- Page records: `magsearch.models.Page` has `magazine_id`, `page_number`,
  `image_path` (e.g. `ace-1987-10/pages/0001.webp`), `thumb_path`.
  `magsearch.models.Magazine` has `id`, `title`, `page_count`.
- `static/` is mounted at `/static`. There are **no** `.js` files there yet;
  `reader.js` will be the first.
- Tests live in `tests/`, use pytest, and share fixtures from
  `tests/conftest.py`. The `app_client` fixture builds a TestClient with
  `auth_enabled=True` and two seeded magazines: `byte-1985-12` (2 pages) and
  `compute-1984-06` (1 page). `conftest.create_user(...)` and
  `conftest.login(client, username, pw)` create and sign in users.
- There is **no** JavaScript test harness (no `package.json`). Server-rendered
  output and endpoints are covered by pytest; `reader.js` behavior is verified
  manually via the protocol in Task 6.

Run the test suite with: `python -m pytest -q` (from the repo root, inside the
project venv).

---

## File Structure

- **Create** `src/magsearch/web/templates/reader.html` — standalone full-viewport
  reader document. Does NOT extend `base.html`. Seeds reader config (magazine id,
  start page, page count, `q`) and loads `reader.js`.
- **Create** `src/magsearch/web/static/reader.js` — all reader behavior: fetch
  page list, build the prev/current/next carousel, handle swipe + tap-zone
  flipping, pinch/double-tap zoom with pan, gesture arbitration, auto-hiding
  overlay, keyboard nav.
- **Modify** `src/magsearch/web/routes.py` — add `from magsearch.web.deps import
  get_db, require_user`; add the `pages.json` endpoint and the `read/{page}`
  route.
- **Modify** `src/magsearch/web/templates/page.html` — add one "Read fullscreen"
  icon-button in the existing control row.
- **Create** `tests/test_web_reader.py` — server tests for both new endpoints
  and the entry button.

---

## Task 1: `pages.json` endpoint

Returns the ordered page list for a magazine, gated to authenticated users.

**Files:**
- Modify: `src/magsearch/web/routes.py`
- Test: `tests/test_web_reader.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_reader.py`:

```python
from tests.conftest import create_user, login
from magsearch.db import make_engine, make_session_factory
from magsearch.settings import get_settings


def _factory():
    settings = get_settings()
    return make_session_factory(make_engine(settings.database_url))


def _login_bob(client):
    create_user(_factory(), "bob", "bob-pw", is_admin=False)
    login(client, "bob", "bob-pw")


def test_pages_json_requires_login(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/pages.json", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_pages_json_returns_ordered_pages(app_client):
    client, _ = app_client
    _login_bob(client)
    resp = client.get("/magazine/byte-1985-12/pages.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["page_count"] == 2
    assert body["pages"] == [
        {"n": 1, "image_path": "byte-1985-12/pages/0001.webp"},
        {"n": 2, "image_path": "byte-1985-12/pages/0002.webp"},
    ]


def test_pages_json_404_for_unknown_magazine(app_client):
    client, _ = app_client
    _login_bob(client)
    resp = client.get("/magazine/does-not-exist/pages.json")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_web_reader.py -q`
Expected: FAIL — the route does not exist yet (the login-required test will get
404 instead of 303; the others 404/assertion-fail).

- [ ] **Step 3: Add the `require_user` import**

In `src/magsearch/web/routes.py`, change the deps import line:

```python
from magsearch.web.deps import get_db, require_user
```

- [ ] **Step 4: Implement the endpoint**

Add to `src/magsearch/web/routes.py` (place it just above the existing
`page_view` route at `routes.py:278` so the magazine routes stay grouped):

```python
@router.get("/magazine/{magazine_id}/pages.json")
def magazine_pages_json(
    magazine_id: str,
    db: Session = Depends(get_db),
    user=Depends(require_user),
) -> dict:
    mag = db.get(Magazine, magazine_id)
    if mag is None:
        raise HTTPException(status_code=404, detail="magazine not found")
    pages = db.scalars(
        select(PageModel)
        .where(PageModel.magazine_id == magazine_id)
        .order_by(PageModel.page_number)
    ).all()
    return {
        "page_count": mag.page_count,
        "pages": [
            {"n": p.page_number, "image_path": p.image_path} for p in pages
        ],
    }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_web_reader.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add src/magsearch/web/routes.py tests/test_web_reader.py
git commit -m "feat: add authed pages.json endpoint for the reader"
```

---

## Task 2: Reader route + minimal template

Renders the standalone reader document, gated to authenticated users, with page
clamping and `q` pass-through. The template starts as a minimal skeleton that
seeds config and loads `reader.js`; the JS itself is built in Task 6.

**Files:**
- Modify: `src/magsearch/web/routes.py`
- Create: `src/magsearch/web/templates/reader.html`
- Test: `tests/test_web_reader.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web_reader.py`:

```python
def test_reader_requires_login(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/read/1", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_reader_renders_for_logged_in_user(app_client):
    client, _ = app_client
    _login_bob(client)
    resp = client.get("/magazine/byte-1985-12/read/1")
    assert resp.status_code == 200
    # Seeds config the JS reads.
    assert '"magazine_id": "byte-1985-12"' in resp.text
    assert '"start_page": 1' in resp.text
    assert '"page_count": 2' in resp.text
    # References the page-list endpoint and the static module.
    assert "/magazine/byte-1985-12/pages.json" in resp.text
    assert "/static/reader.js" in resp.text
    # Standalone document: no site masthead.
    assert "The Archive" not in resp.text


def test_reader_clamps_out_of_range_page(app_client):
    client, _ = app_client
    _login_bob(client)
    # Page 999 of a 2-page issue clamps to the last page.
    resp = client.get("/magazine/byte-1985-12/read/999")
    assert resp.status_code == 200
    assert '"start_page": 2' in resp.text
    # Page 0 clamps up to 1.
    resp = client.get("/magazine/byte-1985-12/read/0")
    assert resp.status_code == 200
    assert '"start_page": 1' in resp.text


def test_reader_404_for_unknown_magazine(app_client):
    client, _ = app_client
    _login_bob(client)
    resp = client.get("/magazine/does-not-exist/read/1")
    assert resp.status_code == 404


def test_reader_carries_query(app_client):
    client, _ = app_client
    _login_bob(client)
    resp = client.get("/magazine/byte-1985-12/read/1?q=synthesizer")
    assert resp.status_code == 200
    assert '"q": "synthesizer"' in resp.text
```

Note: `read/0` is reachable because the path converter accepts any int; we clamp
in the handler rather than rejecting. (If you prefer a 404 for page 0, change the
test and handler together — but clamping matches the spec.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_web_reader.py -q`
Expected: FAIL — the `read/{page}` route and `reader.html` do not exist yet.

- [ ] **Step 3: Create the minimal reader template**

Create `src/magsearch/web/templates/reader.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<title>{{ mag.title }} · reading</title>
<style>
  /* The reader owns the whole viewport; no scrolling, no rubber-banding. */
  html, body {
    margin: 0;
    height: 100%;
    background: #14110c;
    overflow: hidden;
    overscroll-behavior: none;
    -webkit-user-select: none;
    user-select: none;
  }
</style>
</head>
<body>
  <!-- reader.js reads this config and the pages.json endpoint, then builds the
       carousel into #reader-root. -->
  <div id="reader-root"></div>
  <script id="reader-config" type="application/json">
    {{ config_json | safe }}
  </script>
  <script src="/static/reader.js" defer></script>
</body>
</html>
```

- [ ] **Step 4: Implement the route**

Add to `src/magsearch/web/routes.py`, directly below the `pages.json` endpoint
from Task 1. Add `import json` at the top of the file if it is not already
imported (check the existing imports first).

```python
@router.get("/magazine/{magazine_id}/read/{page}", response_class=HTMLResponse)
def reader_view(
    request: Request,
    magazine_id: str,
    page: int,
    q: str = Query(default=""),
    db: Session = Depends(get_db),
    user=Depends(require_user),
) -> HTMLResponse:
    mag = db.get(Magazine, magazine_id)
    if mag is None:
        raise HTTPException(status_code=404, detail="magazine not found")
    # Clamp into 1..page_count rather than 404 — a stale/over-shot page
    # number should still open the reader at a valid page.
    start_page = max(1, min(page, mag.page_count))
    config = {
        "magazine_id": magazine_id,
        "start_page": start_page,
        "page_count": mag.page_count,
        "q": q,
        "pages_url": f"/magazine/{magazine_id}/pages.json",
        "bundle_base": "/bundle/",
        "page_url_base": f"/magazine/{magazine_id}/page/",
    }
    return _TEMPLATES.TemplateResponse(
        request,
        "reader.html",
        {"mag": mag, "config_json": json.dumps(config)},
    )
```

Note on `config_json`: it is rendered with `| safe` inside a
`<script type="application/json">` block, so it must be valid JSON. `json.dumps`
escapes `<`, `>`, `&` poorly by default — to be safe against a magazine title or
query containing `</script>`, set `json.dumps(config).replace("<", "\\u003c")`.
Apply that in the handler:

```python
        "config_json": json.dumps(config).replace("<", "\\u003c"),
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_web_reader.py -q`
Expected: PASS (all reader + pages.json tests green).

- [ ] **Step 6: Commit**

```bash
git add src/magsearch/web/routes.py src/magsearch/web/templates/reader.html tests/test_web_reader.py
git commit -m "feat: add authed fullscreen reader route and skeleton template"
```

---

## Task 3: "Read fullscreen" button on the page viewer

Add a single icon-button to the existing control row in `page.html` linking to
the reader at the current page, carrying `q`.

**Files:**
- Modify: `src/magsearch/web/templates/page.html` (control row around lines
  27–68; the `?q=` query-string helper `_qs` is defined later at
  `page.html:174`, but the control row already builds `?q=` inline as the
  prev/next links do — match that style).
- Test: `tests/test_web_reader.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web_reader.py`:

```python
def test_page_viewer_has_reader_button(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/page/2")
    assert resp.status_code == 200
    assert "/magazine/byte-1985-12/read/2" in resp.text


def test_page_viewer_reader_button_carries_query(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/page/1?q=synthesizer")
    assert resp.status_code == 200
    assert "/magazine/byte-1985-12/read/1?q=synthesizer" in resp.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_web_reader.py -q -k reader_button`
Expected: FAIL — the link is not in the template yet.

- [ ] **Step 3: Add the button to the control row**

In `src/magsearch/web/templates/page.html`, inside the control row, add the
button just before the `{% if has_next %}` block (around line 61), so it sits
with the other icon buttons. Match the existing `icon-btn` + inline-`?q=` style:

```html
      <a href="/magazine/{{ mag.id }}/read/{{ page.page_number }}{% if q %}?q={{ q|urlencode }}{% endif %}"
         class="icon-btn" title="Read fullscreen (F)" aria-label="Read fullscreen">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/></svg>
      </a>
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_web_reader.py -q -k reader_button`
Expected: PASS (2 passed).

- [ ] **Step 5: Optional — wire the `F` keyboard shortcut**

The page viewer already has a `keydown` handler (`page.html:430`). To let `F`
open the reader, add this branch inside that handler, after the existing `h`/`H`
branch (before the closing of the listener). It reuses the same link the button
points at by reading it from the DOM:

```javascript
      } else if (e.key === "f" || e.key === "F") {
        var readerLink = document.querySelector('a[aria-label="Read fullscreen"]');
        if (readerLink) {
          e.preventDefault();
          window.location.href = readerLink.getAttribute("href");
        }
      }
```

This step has no automated test (it is browser keyboard behavior); verify it
manually in Task 6. If you skip it, remove "(F)" from the button `title` in
Step 3 to avoid advertising a shortcut that does nothing.

- [ ] **Step 6: Commit**

```bash
git add src/magsearch/web/templates/page.html tests/test_web_reader.py
git commit -m "feat: add Read fullscreen entry button to the page viewer"
```

---

## Task 4: Reader carousel — page list, render, and swipe flipping

Build the core of `reader.js`: read config, fetch the page list, build the
prev/current/next carousel, and flip via swipe with a pure swipe-decision
function. Zoom, tap zones, overlay, and keyboard come in Task 5.

**Files:**
- Create: `src/magsearch/web/static/reader.js`

There is no JS test harness, so this task has no pytest steps. Keep the
swipe-decision logic in a pure function (`decideFlip`) with the reasoning in
comments so it is reviewable, and verify behavior in Task 6.

- [ ] **Step 1: Write `reader.js` — config, page list, carousel, swipe**

Create `src/magsearch/web/static/reader.js`:

```javascript
(function () {
  "use strict";

  var cfgEl = document.getElementById("reader-config");
  if (!cfgEl) return;
  var CFG = JSON.parse(cfgEl.textContent);
  var root = document.getElementById("reader-root");

  // ─── pure decision logic (reviewable without a browser) ──────────────────
  // Decide whether a horizontal drag should flip, and in which direction.
  // dx: total horizontal movement in px (positive = finger moved right).
  // vx: horizontal velocity in px/ms at release (sign matches dx).
  // width: viewport width in px.
  // Returns -1 (go to previous), +1 (go to next), or 0 (spring back).
  // A flip fires when the drag passed ~25% of the width OR was a fast flick.
  function decideFlip(dx, vx, width) {
    var DISTANCE = width * 0.25;
    var VELOCITY = 0.5; // px/ms
    var far = Math.abs(dx) > DISTANCE;
    var fast = Math.abs(vx) > VELOCITY;
    if (!far && !fast) return 0;
    // Finger moving right reveals the previous page (track shifts right).
    return dx > 0 ? -1 : 1;
  }

  // ─── reader state ────────────────────────────────────────────────────────
  var pages = null;            // [{n, image_path}], filled by loadPages()
  var current = CFG.start_page; // 1-based page number currently centered
  var stage, track;            // DOM: clipping stage + the 3-slide track
  var dragging = false;

  function imgUrl(pageNumber) {
    var p = pages && pages[pageNumber - 1];
    return p ? CFG.bundle_base + p.image_path : null;
  }

  // Build the stage and the 3-slide track. Slides are positioned at -100%, 0,
  // +100% so the centre slide fills the viewport and neighbours sit just off
  // each edge, ready to slide in.
  function buildDom() {
    stage = document.createElement("div");
    stage.id = "reader-stage";
    track = document.createElement("div");
    track.id = "reader-track";
    for (var i = 0; i < 3; i++) {
      var slide = document.createElement("div");
      slide.className = "reader-slide";
      var img = document.createElement("img");
      img.className = "reader-img";
      img.draggable = false;
      img.alt = "";
      slide.appendChild(img);
      track.appendChild(slide);
    }
    stage.appendChild(track);
    root.appendChild(stage);
  }

  // Point the three slide <img>s at prev/current/next and reset the track
  // transform to centred. Missing neighbours (before page 1 / after the last
  // page) get a blank src and are flagged so we don't over-swipe past an end.
  function syncSlides() {
    var slides = track.children;
    var nums = [current - 1, current, current + 1];
    for (var i = 0; i < 3; i++) {
      var n = nums[i];
      var img = slides[i].firstChild;
      var url = (n >= 1 && n <= CFG.page_count) ? imgUrl(n) : null;
      slides[i].dataset.empty = url ? "" : "1";
      if (url) {
        if (img.getAttribute("src") !== url) img.setAttribute("src", url);
      } else {
        img.removeAttribute("src");
      }
    }
    setTrackX(0, false);
  }

  // Translate the track. animate=true uses a CSS transition; during a finger
  // drag we set animate=false so it tracks 1:1.
  function setTrackX(px, animate) {
    track.style.transition = animate ? "transform 0.25s ease" : "none";
    // The centre slide is the second of three, so the resting offset is -100%.
    track.style.transform = "translateX(calc(-100% + " + px + "px))";
  }

  function canGo(dir) {
    var target = current + dir;
    return target >= 1 && target <= CFG.page_count;
  }

  // Flip by dir (-1 prev, +1 next). Animate the track fully off, then re-centre
  // on the new page and re-point the slides (which restores the buffer).
  function flip(dir) {
    if (!canGo(dir)) { setTrackX(0, true); return; }
    var width = stage.clientWidth;
    setTrackX(-dir * width, true);
    window.setTimeout(function () {
      current += dir;
      onPageChanged();
      syncSlides(); // re-centres (animate=false) and rebuilds the buffer
    }, 250);
  }

  // Hook for Task 5 (reset zoom, update page indicator). Safe no-op for now.
  function onPageChanged() {}

  // ─── swipe handling via Pointer Events ───────────────────────────────────
  var startX = 0, startY = 0, startT = 0, lastX = 0, lastT = 0, axis = null;
  function bindSwipe() {
    stage.addEventListener("pointerdown", function (e) {
      if (e.isPrimary === false) return;
      dragging = true; axis = null;
      startX = lastX = e.clientX; startY = e.clientY;
      startT = lastT = e.timeStamp;
      stage.setPointerCapture(e.pointerId);
    });
    stage.addEventListener("pointermove", function (e) {
      if (!dragging) return;
      var dx = e.clientX - startX, dy = e.clientY - startY;
      // Lock to an axis on first meaningful movement so vertical scrolling
      // intent doesn't drag the page sideways.
      if (axis === null && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) {
        axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
      }
      if (axis === "x") {
        // Resist dragging past an end (no neighbour to reveal).
        if ((dx > 0 && !canGo(-1)) || (dx < 0 && !canGo(1))) dx *= 0.3;
        setTrackX(dx, false);
      }
      lastX = e.clientX; lastT = e.timeStamp;
    });
    var end = function (e) {
      if (!dragging) return;
      dragging = false;
      try { stage.releasePointerCapture(e.pointerId); } catch (_) {}
      if (axis !== "x") { setTrackX(0, false); return; }
      var dx = e.clientX - startX;
      var dt = Math.max(1, e.timeStamp - lastT);
      var vx = (e.clientX - lastX) / dt;
      var dir = decideFlip(dx, vx, stage.clientWidth);
      if (dir !== 0 && canGo(dir)) flip(dir);
      else setTrackX(0, true);
    };
    stage.addEventListener("pointerup", end);
    stage.addEventListener("pointercancel", end);
  }

  // ─── styles ───────────────────────────────────────────────────────────────
  function injectStyles() {
    var css = [
      "#reader-stage{position:fixed;inset:0;overflow:hidden;touch-action:none;}",
      "#reader-track{position:absolute;inset:0;display:flex;will-change:transform;}",
      ".reader-slide{position:relative;flex:0 0 100%;height:100%;}",
      ".reader-img{position:absolute;inset:0;margin:auto;max-width:100%;max-height:100%;" +
        "object-fit:contain;-webkit-user-drag:none;}"
    ].join("");
    var style = document.createElement("style");
    style.textContent = css;
    document.head.appendChild(style);
  }

  // ─── boot ──────────────────────────────────────────────────────────────────
  function loadPages() {
    return fetch(CFG.pages_url, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (data) { pages = data.pages || []; });
  }

  injectStyles();
  buildDom();
  bindSwipe();
  loadPages().then(function () {
    syncSlides();
  }).catch(function () {
    root.textContent = "Could not load this issue.";
  });

  // Expose internals for Task 5 to extend without re-reading the DOM.
  window.__reader = {
    get current() { return current; },
    set current(v) { current = v; },
    flip: flip,
    canGo: canGo,
    get pageCount() { return CFG.page_count; },
    onPageChanged: function (fn) { onPageChanged = fn; }
  };
})();
```

- [ ] **Step 2: Smoke-check the file parses**

Run: `node --check src/magsearch/web/static/reader.js` if Node is available;
otherwise open the reader in a browser (Task 6) and confirm there are no console
syntax errors. (Node is not a project dependency — skip if absent.)

- [ ] **Step 3: Commit**

```bash
git add src/magsearch/web/static/reader.js
git commit -m "feat: reader carousel with swipe-to-flip page navigation"
```

---

## Task 5: Overlay, tap zones, pinch-zoom, and keyboard

Extend `reader.js` with the auto-hiding overlay (exit + page indicator), tap
zones, pinch/double-tap zoom with pan, gesture arbitration, and keyboard nav.

**Files:**
- Modify: `src/magsearch/web/static/reader.js`

- [ ] **Step 1: Add the overlay bar and page indicator**

Inside the IIFE, after `buildDom()` defines `stage`, add an overlay builder and
call it during boot (call `buildOverlay()` right after `buildDom()`):

```javascript
  var overlay, pageLabel, hideTimer = null;
  function buildOverlay() {
    overlay = document.createElement("div");
    overlay.id = "reader-overlay";
    var exit = document.createElement("a");
    exit.id = "reader-exit";
    exit.setAttribute("aria-label", "Exit reader");
    exit.textContent = "✕";
    exit.href = CFG.page_url_base + current + (CFG.q ? "?q=" + encodeURIComponent(CFG.q) : "");
    pageLabel = document.createElement("div");
    pageLabel.id = "reader-pagelabel";
    overlay.appendChild(exit);
    overlay.appendChild(pageLabel);
    root.appendChild(overlay);
    // Keep the exit link pointing at the page the reader is currently on.
    window.__reader.onPageChanged(function () {
      exit.href = CFG.page_url_base + current + (CFG.q ? "?q=" + encodeURIComponent(CFG.q) : "");
      updatePageLabel();
    });
    updatePageLabel();
  }
  function updatePageLabel() {
    if (pageLabel) pageLabel.textContent = "p. " + current + " / " + CFG.page_count;
  }
  function showOverlay() {
    if (!overlay) return;
    overlay.classList.add("is-visible");
    if (hideTimer) window.clearTimeout(hideTimer);
    hideTimer = window.setTimeout(function () {
      overlay.classList.remove("is-visible");
    }, 3000);
  }
```

Add these rules to the `css` array in `injectStyles()`:

```javascript
      "#reader-overlay{position:fixed;top:0;left:0;right:0;display:flex;" +
        "align-items:center;justify-content:space-between;padding:14px 18px;" +
        "color:#f1e9d6;font:600 14px/1 system-ui,sans-serif;letter-spacing:.04em;" +
        "background:linear-gradient(#14110ccc,#14110c00);opacity:0;" +
        "transition:opacity .2s ease;pointer-events:none;z-index:10;}",
      "#reader-overlay.is-visible{opacity:1;pointer-events:auto;}",
      "#reader-exit{color:#f1e9d6;text-decoration:none;font-size:22px;line-height:1;}",
```

- [ ] **Step 2: Add tap zones**

A tap (pointerdown→up with negligible movement) on the left/right quarter flips;
a centre tap toggles the overlay. Add this to `bindSwipe`'s `end` handler — at
the very top of `end`, before the drag logic, detect a tap:

```javascript
    var end = function (e) {
      if (!dragging) return;
      var movedX = Math.abs(e.clientX - startX);
      var movedY = Math.abs(e.clientY - startY);
      var elapsed = e.timeStamp - startT;
      var isTap = movedX < 8 && movedY < 8 && elapsed < 300;
      if (isTap && zoom === 1) {
        dragging = false;
        try { stage.releasePointerCapture(e.pointerId); } catch (_) {}
        var w = stage.clientWidth;
        if (e.clientX < w * 0.25) { if (canGo(-1)) flip(-1); }
        else if (e.clientX > w * 0.75) { if (canGo(1)) flip(1); }
        else { showOverlay(); }
        return;
      }
      // …existing drag-release logic from Task 4 follows…
```

Note: `zoom` is introduced in Step 3; if you implement Step 2 before Step 3,
temporarily treat `zoom` as `1`. Implementing Step 3 first is fine too.

- [ ] **Step 3: Add pinch/double-tap zoom with pan and gesture arbitration**

Add zoom state near the other state vars (`var pages = null; …`):

```javascript
  var zoom = 1, panX = 0, panY = 0;        // current-page zoom transform
  var ZOOM_IN = 2.5;                        // double-tap zoom level
  var activePointers = {};                  // id -> {x, y} for pinch tracking
  var pinchStartDist = 0, pinchStartZoom = 1;
```

Add a helper that applies the zoom transform to the **centre** slide's image:

```javascript
  function centerImg() {
    return track.children[1].firstChild;
  }
  function applyZoom() {
    var img = centerImg();
    if (!img) return;
    img.style.transform =
      "translate(" + panX + "px," + panY + "px) scale(" + zoom + ")";
    img.style.transition = "transform .15s ease";
    stage.dataset.zoomed = zoom > 1 ? "1" : "";
  }
  function resetZoom() { zoom = 1; panX = 0; panY = 0; applyZoom(); }
```

Reset zoom on every page change — set the Task 4 `onPageChanged` no-op to call
`resetZoom` by registering through the exposed hook during boot:

```javascript
  window.__reader.onPageChanged(function () { resetZoom(); });
```

(If Step 1's overlay also registers an `onPageChanged`, keep both: change
`onPageChanged` in Task 4 to support multiple callbacks, or call `resetZoom()`
and `updatePageLabel()` from a single registered function. Simplest: make the
Task 4 `onPageChanged` hook store an array — see Step 5 cleanup.)

**This step rewrites `bindSwipe` wholesale**, and the replacement below already
includes the tap-zone logic from Step 2 (in the `end` handler). If you are
applying the steps in order, this supersedes the Step 2 edit — paste this whole
`bindSwipe` rather than layering on top of Step 2. (Step 2 is kept as a separate
step only to explain the tap-zone behavior on its own.)

Pinch + double-tap handling. Augment the pointer handlers in `bindSwipe`:

- In `pointerdown`: record the pointer in `activePointers`. If two pointers are
  down, capture `pinchStartDist` (distance between them) and `pinchStartZoom`.
  Also detect a double-tap (two taps < 300ms apart) to toggle between `zoom===1`
  and `ZOOM_IN`.
- In `pointermove`: if two pointers are down, set
  `zoom = clamp(pinchStartZoom * (dist / pinchStartDist), 1, 4)` and
  `applyZoom()`. If one pointer is down **and** `zoom > 1`, pan instead of
  swiping: `panX += dx; panY += dy; applyZoom()`.
- In `end`/`pointerup`/`pointercancel`: delete the pointer from
  `activePointers`. When dropping back to `zoom === 1`, clamp pan to 0.

Concretely, replace the `bindSwipe` body with this expanded version:

```javascript
  function dist(a, b) {
    var dx = a.x - b.x, dy = a.y - b.y;
    return Math.sqrt(dx * dx + dy * dy);
  }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  var lastTapT = 0;
  function bindSwipe() {
    stage.addEventListener("pointerdown", function (e) {
      activePointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      var ids = Object.keys(activePointers);
      if (ids.length === 2) {
        var p = activePointers[ids[0]], q = activePointers[ids[1]];
        pinchStartDist = dist(p, q) || 1;
        pinchStartZoom = zoom;
        dragging = false; // pinch suspends swipe
        return;
      }
      // double-tap to toggle zoom
      var now = e.timeStamp;
      if (now - lastTapT < 300) {
        zoom = (zoom === 1) ? ZOOM_IN : 1;
        if (zoom === 1) { panX = 0; panY = 0; }
        applyZoom();
        lastTapT = 0;
        return;
      }
      lastTapT = now;
      dragging = true; axis = null;
      startX = lastX = e.clientX; startY = e.clientY;
      startT = lastT = e.timeStamp;
      stage.setPointerCapture(e.pointerId);
    });

    stage.addEventListener("pointermove", function (e) {
      if (activePointers[e.pointerId]) {
        activePointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      }
      var ids = Object.keys(activePointers);
      if (ids.length === 2) {
        var p = activePointers[ids[0]], q = activePointers[ids[1]];
        zoom = clamp(pinchStartZoom * (dist(p, q) / pinchStartDist), 1, 4);
        applyZoom();
        return;
      }
      if (!dragging) return;
      var dx = e.clientX - startX, dy = e.clientY - startY;
      if (zoom > 1) {
        // Panning the zoomed page; swipe-to-flip is suspended.
        panX += (e.clientX - lastX);
        panY += (e.clientY - lastY());
        applyZoom();
        lastX = e.clientX; lastTY = e.clientY; lastT = e.timeStamp;
        return;
      }
      if (axis === null && (Math.abs(dx) > 8 || Math.abs(dy) > 8)) {
        axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
      }
      if (axis === "x") {
        if ((dx > 0 && !canGo(-1)) || (dx < 0 && !canGo(1))) dx *= 0.3;
        setTrackX(dx, false);
      }
      lastX = e.clientX; lastT = e.timeStamp;
    });

    var end = function (e) {
      delete activePointers[e.pointerId];
      if (!dragging) {
        try { stage.releasePointerCapture(e.pointerId); } catch (_) {}
        return;
      }
      var movedX = Math.abs(e.clientX - startX);
      var movedY = Math.abs(e.clientY - startY);
      var elapsed = e.timeStamp - startT;
      var isTap = movedX < 8 && movedY < 8 && elapsed < 300;
      dragging = false;
      try { stage.releasePointerCapture(e.pointerId); } catch (_) {}
      if (isTap && zoom === 1) {
        var w = stage.clientWidth;
        if (e.clientX < w * 0.25) { if (canGo(-1)) flip(-1); }
        else if (e.clientX > w * 0.75) { if (canGo(1)) flip(1); }
        else { showOverlay(); }
        return;
      }
      if (zoom > 1) return;            // pan release: nothing to snap
      if (axis !== "x") { setTrackX(0, false); return; }
      var dx = e.clientX - startX;
      var dt = Math.max(1, e.timeStamp - lastT);
      var vx = (e.clientX - lastX) / dt;
      var dir = decideFlip(dx, vx, stage.clientWidth);
      if (dir !== 0 && canGo(dir)) flip(dir);
      else setTrackX(0, true);
    };
    stage.addEventListener("pointerup", end);
    stage.addEventListener("pointercancel", end);
  }
```

Note: the pan branch references vertical movement. Track the previous Y with a
dedicated `lastTY` variable (initialise `var lastTY = 0;` with the other state
vars) and replace `lastY()` with arithmetic using `lastTY`:

```javascript
      if (zoom > 1) {
        panX += (e.clientX - lastX);
        panY += (e.clientY - lastTY);
        applyZoom();
        lastX = e.clientX; lastTY = e.clientY; lastT = e.timeStamp;
        return;
      }
```

and set `lastTY = e.clientY;` in `pointerdown` next to `lastX = e.clientX;`.

- [ ] **Step 4: Add keyboard navigation**

After `bindSwipe()` in the boot section, add:

```javascript
  document.addEventListener("keydown", function (e) {
    if (e.key === "ArrowLeft") { if (canGo(-1)) flip(-1); }
    else if (e.key === "ArrowRight") { if (canGo(1)) flip(1); }
    else if (e.key === "Escape") {
      window.location.href =
        CFG.page_url_base + current + (CFG.q ? "?q=" + encodeURIComponent(CFG.q) : "");
    }
  });
```

- [ ] **Step 5: Reconcile the `onPageChanged` callbacks**

Both the overlay (Step 1) and zoom reset (Step 3) need to run on a page change.
Replace the Task 4 single-callback hook with a list. In Task 4's code, change:

```javascript
  function onPageChanged() {}
```

to:

```javascript
  var pageChangeCbs = [];
  function onPageChanged() { for (var i = 0; i < pageChangeCbs.length; i++) pageChangeCbs[i](); }
```

and change the exposed hook:

```javascript
    onPageChanged: function (fn) { pageChangeCbs.push(fn); }
```

Now Step 1 and Step 3 can each register independently via
`window.__reader.onPageChanged(...)`.

- [ ] **Step 6: Smoke-check the file parses**

Run: `node --check src/magsearch/web/static/reader.js` if Node is available.
Otherwise rely on the browser console in Task 6.

- [ ] **Step 7: Commit**

```bash
git add src/magsearch/web/static/reader.js
git commit -m "feat: reader overlay, tap zones, pinch-zoom, and keyboard nav"
```

---

## Task 6: Manual verification protocol

No automated browser tests exist in this repo, so verify the reader by hand.
Use a real tablet or a desktop browser's device-emulation mode with touch
simulation (Chrome DevTools → toggle device toolbar → set a touch device).

**Files:** none (verification only).

- [ ] **Step 1: Start the app and sign in**

```bash
python -m magsearch.cli web   # or the documented dev server command; check README
```

Open the site, sign in as a user (create one via the admin UI or a script if
needed), and navigate to a magazine page, e.g. `/magazine/<id>/page/1`.

- [ ] **Step 2: Verify entry**

Confirm the "Read fullscreen" icon-button appears in the page-viewer control
row. Click it. The URL becomes `/magazine/<id>/read/1` and a full-viewport dark
reader appears showing the whole page. If you opened the page with `?q=term`,
confirm the reader URL carries `?q=term`.

- [ ] **Step 3: Verify swipe flipping**

Swipe left → next page slides in and tracks your finger; release past ~¼ width
(or flick) to commit, otherwise it springs back. Swipe right → previous page.
On page 1, swiping right resists and springs back (no wrap). On the last page,
swiping left resists and springs back.

- [ ] **Step 4: Verify tap zones and overlay**

Tap the left quarter → previous page; tap the right quarter → next page; tap the
centre → the overlay bar (✕ and "p. N / total") fades in and auto-hides after
~3s. Confirm the page indicator updates as you flip.

- [ ] **Step 5: Verify zoom and arbitration**

Pinch to zoom in; drag with one finger to pan around the zoomed page (it should
NOT flip while zoomed). Pinch back / double-tap to return to fit; confirm
one-finger drag flips again. Flip to another page and confirm zoom has reset.

- [ ] **Step 6: Verify exit and keyboard**

Tap ✕ (or press Esc) → returns to `/magazine/<id>/page/N` for the page you were
on, with `?q=` preserved if present. With a keyboard attached, ←/→ flip pages.
If you implemented Task 3 Step 5, confirm `F` on the page viewer opens the
reader.

- [ ] **Step 7: Run the full server test suite**

Run: `python -m pytest -q`
Expected: all tests pass (including `tests/test_web_reader.py` and the existing
`tests/test_web_page.py`).

- [ ] **Step 8: Final commit (if any tweaks were needed)**

```bash
git add -A
git commit -m "fix: reader adjustments from manual verification"
```

---

## Self-Review notes (for the implementer)

- **Auth:** Both new endpoints use `Depends(require_user)`, which enforces login
  even when the global `require_login` flag is off — matching the spec's "only
  authenticated users may access the new endpoints." Verified by
  `test_pages_json_requires_login` and `test_reader_requires_login`.
- **Spec coverage:** entry button (Task 3), single-page letterboxing (Task 2
  template + Task 4 CSS), sliding carousel (Task 4), swipe + tap zones (Tasks
  4–5), pinch-zoom + arbitration (Task 5), auto-hiding overlay + exit + page
  indicator (Task 5), `q` pass-through + Exit to viewer (Tasks 2 & 5), `pages.json`
  from DB (Task 1), clamping/404 (Tasks 1–2). In-reader highlighting is
  intentionally out of scope per the spec.
- **No JS tests:** acknowledged; client logic is kept in pure functions
  (`decideFlip`) and verified via the Task 6 protocol.
```
