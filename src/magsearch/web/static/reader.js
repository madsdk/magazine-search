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
  var pagesByNum = null;       // {n: page}, built alongside pages in loadPages()
  var current = CFG.start_page; // 1-based page number currently centered
  var stage, track;            // DOM: clipping stage + the 3-slide track
  var dragging = false;
  var animating = false;

  // ─── overlay + zoom state ──────────────────────────────────────────────────
  var overlay, pageLabel, hideTimer = null;
  var zoom = 1, panX = 0, panY = 0;
  var ZOOM_IN = 2.5;
  var activePointers = {};
  var pinchStartDist = 0, pinchStartZoom = 1;
  var lastTY = 0;          // last clientY, used while panning a zoomed page
  var lastTapT = 0;        // timestamp of last tap, for double-tap detection

  function imgUrl(pageNumber) {
    var p = pagesByNum && pagesByNum[pageNumber];
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

  var FLIP_MS = 250; // slide animation duration; CSS transition and the post-flip recentre timeout must agree

  // Translate the track. animate=true uses a CSS transition; during a finger
  // drag we set animate=false so it tracks 1:1.
  function setTrackX(px, animate) {
    track.style.transition = animate ? ("transform " + (FLIP_MS / 1000) + "s ease") : "none";
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
    if (animating) return;
    if (!canGo(dir)) { setTrackX(0, true); return; }
    var width = stage.clientWidth;
    animating = true;
    setTrackX(-dir * width, true);
    window.setTimeout(function () {
      current += dir;
      // syncSlides() runs BEFORE onPageChanged() so the slide <img>s are
      // re-pointed at the new prev/current/next first; the callbacks (which
      // include resetZoom) then operate on the NEW centre image.
      syncSlides(); // re-centres (animate=false) and rebuilds the buffer
      onPageChanged();
      animating = false;
    }, FLIP_MS);
  }

  // Hook for page-change side effects (reset zoom, update page indicator).
  // Supports multiple registered callbacks; runs them in registration order.
  var pageChangeCbs = [];
  function onPageChanged() {
    for (var i = 0; i < pageChangeCbs.length; i++) pageChangeCbs[i]();
  }

  // ─── overlay (auto-hiding top bar with exit + page indicator) ──────────────
  function buildOverlay() {
    overlay = document.createElement("div");
    overlay.id = "reader-overlay";
    var exit = document.createElement("a");
    exit.id = "reader-exit";
    exit.setAttribute("aria-label", "Exit reader");
    exit.textContent = "✕";
    exit.href = exitHref();
    pageLabel = document.createElement("div");
    pageLabel.id = "reader-pagelabel";
    overlay.appendChild(exit);
    overlay.appendChild(pageLabel);
    root.appendChild(overlay);
    pageChangeCbs.push(function () {
      exit.href = exitHref();
      updatePageLabel();
    });
    updatePageLabel();
  }
  function exitHref() {
    return CFG.page_url_base + current + (CFG.q ? "?q=" + encodeURIComponent(CFG.q) : "");
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

  // ─── zoom helpers ──────────────────────────────────────────────────────────
  function centerImg() { return track.children[1].firstChild; }
  function applyZoom() {
    var img = centerImg();
    if (!img) return;
    img.style.transform = "translate(" + panX + "px," + panY + "px) scale(" + zoom + ")";
    img.style.transition = "transform .15s ease";
    stage.dataset.zoomed = zoom > 1 ? "1" : "";
  }
  function resetZoom() { zoom = 1; panX = 0; panY = 0; applyZoom(); }
  function dist(a, b) { var dx = a.x - b.x, dy = a.y - b.y; return Math.sqrt(dx * dx + dy * dy); }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  // ─── swipe / tap / pinch / pan handling via Pointer Events ────────────────
  var startX = 0, startY = 0, lastX = 0, lastT = 0, axis = null;
  var vx = 0;       // instantaneous horizontal velocity in px/ms, tracked across pointermove samples
  var downT = 0;    // pointerdown timestamp, used only for tap-duration detection
  function bindSwipe() {
    stage.addEventListener("pointerdown", function (e) {
      // INVARIANT 1: never start interaction mid-animation. Returning at the
      // very top (before touching activePointers/zoom/drag state) keeps state
      // clean and blocks drag/tap/double-tap/pinch starts while flipping.
      if (animating) return;

      activePointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      var ids = Object.keys(activePointers);

      // Two fingers down → start a pinch; suspend any one-finger swipe.
      if (ids.length === 2) {
        var p = activePointers[ids[0]], q = activePointers[ids[1]];
        pinchStartDist = dist(p, q) || 1;
        pinchStartZoom = zoom;
        dragging = false;
        return;
      }

      // Ignore spurious non-primary single pointers (the 2-finger case above
      // handles a legitimate second finger).
      if (e.isPrimary === false) return;

      // Double-tap toggles zoom on the current page.
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
      lastT = e.timeStamp; lastTY = e.clientY; downT = e.timeStamp;
      vx = 0;
      stage.setPointerCapture(e.pointerId);
    });

    stage.addEventListener("pointermove", function (e) {
      if (activePointers[e.pointerId]) activePointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      var ids = Object.keys(activePointers);

      // Pinch: rescale relative to the gesture's starting spread.
      if (ids.length === 2) {
        var p = activePointers[ids[0]], q = activePointers[ids[1]];
        zoom = clamp(pinchStartZoom * (dist(p, q) / pinchStartDist), 1, 4);
        applyZoom();
        return;
      }

      if (!dragging) return;
      var dx = e.clientX - startX, dy = e.clientY - startY;

      // ARBITRATION: when zoomed, one-finger drag PANS (swipe suspended).
      if (zoom > 1) {
        panX += (e.clientX - lastX);
        panY += (e.clientY - lastTY);
        applyZoom();
        lastX = e.clientX; lastTY = e.clientY; lastT = e.timeStamp;
        return;
      }

      // INVARIANT 2: per-sample velocity from inter-sample delta, computed
      // BEFORE lastX/lastT are overwritten below.
      var dtMove = e.timeStamp - lastT;
      if (dtMove > 0) vx = (e.clientX - lastX) / dtMove;

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
      lastX = e.clientX; lastTY = e.clientY; lastT = e.timeStamp;
    });

    var end = function (e) {
      delete activePointers[e.pointerId];
      if (!dragging) { try { stage.releasePointerCapture(e.pointerId); } catch (_) {} return; }
      var movedX = Math.abs(e.clientX - startX);
      var movedY = Math.abs(e.clientY - startY);
      var elapsed = e.timeStamp - downT; // downT is the pointerdown time
      var isTap = movedX < 8 && movedY < 8 && elapsed < 300;
      dragging = false;
      try { stage.releasePointerCapture(e.pointerId); } catch (_) {}

      // Tap zones only apply when not zoomed.
      if (isTap && zoom === 1) {
        var w = stage.clientWidth;
        if (e.clientX < w * 0.25) { if (canGo(-1)) flip(-1); }
        else if (e.clientX > w * 0.75) { if (canGo(1)) flip(1); }
        else { showOverlay(); }
        return;
      }
      // A drag that ended while zoomed was a pan; nothing to settle.
      if (zoom > 1) return;
      if (axis !== "x") { setTrackX(0, false); return; }
      var dx = e.clientX - startX;
      // INVARIANT 2/5: consume the per-sample vx via the pure decideFlip.
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
        "object-fit:contain;-webkit-user-drag:none;transform-origin:center center;}",
      "#reader-overlay{position:fixed;top:0;left:0;right:0;display:flex;" +
        "align-items:center;justify-content:space-between;padding:14px 18px;" +
        "color:#f1e9d6;font:600 14px/1 system-ui,sans-serif;letter-spacing:.04em;" +
        "background:linear-gradient(#14110ccc,#14110c00);opacity:0;" +
        "transition:opacity .2s ease;pointer-events:none;z-index:10;}",
      "#reader-overlay.is-visible{opacity:1;pointer-events:auto;}",
      "#reader-exit{color:#f1e9d6;text-decoration:none;font-size:22px;line-height:1;}"
    ].join("");
    var style = document.createElement("style");
    style.textContent = css;
    document.head.appendChild(style);
  }

  // ─── boot ──────────────────────────────────────────────────────────────────
  function loadPages() {
    return fetch(CFG.pages_url, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (data) {
        pages = data.pages || [];
        pagesByNum = {};
        for (var i = 0; i < pages.length; i++) pagesByNum[pages[i].n] = pages[i];
      });
  }

  injectStyles();
  buildDom();
  buildOverlay();
  bindSwipe();

  // Reset zoom whenever we settle on a new page. Registered after buildOverlay
  // and run inside flip()'s timeout AFTER syncSlides(), so it targets the new
  // centre image.
  pageChangeCbs.push(function () { resetZoom(); });

  // Keyboard navigation.
  document.addEventListener("keydown", function (e) {
    if (e.key === "ArrowLeft") { if (canGo(-1)) flip(-1); }
    else if (e.key === "ArrowRight") { if (canGo(1)) flip(1); }
    else if (e.key === "Escape") { window.location.href = exitHref(); }
  });

  loadPages().then(function () {
    syncSlides();
  }).catch(function () {
    root.textContent = "Could not load this issue.";
  });

  // Expose internals for other code to extend without re-reading the DOM.
  window.__reader = {
    get current() { return current; },
    set current(v) { current = v; },
    flip: flip,
    canGo: canGo,
    get pageCount() { return CFG.page_count; },
    onPageChanged: function (fn) { pageChangeCbs.push(fn); }
  };
})();
