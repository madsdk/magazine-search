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

  // Hook for the next task (reset zoom, update page indicator). Safe no-op now.
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

  // Expose internals for the next task to extend without re-reading the DOM.
  window.__reader = {
    get current() { return current; },
    set current(v) { current = v; },
    flip: flip,
    canGo: canGo,
    get pageCount() { return CFG.page_count; },
    onPageChanged: function (fn) { onPageChanged = fn; }
  };
})();
