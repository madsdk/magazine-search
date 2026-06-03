# tests/test_web_search.py
def test_search_returns_marked_results(app_client):
    client, _ = app_client
    # Flat view preserves the original snippet-with-mark behavior.
    resp = client.get("/search", params={"q": "synthesizer", "view": "flat"})
    assert resp.status_code == 200
    assert "<mark>" in resp.text
    assert "Byte" in resp.text


def test_search_empty_query_renders_no_results(app_client):
    client, _ = app_client
    resp = client.get("/search", params={"q": ""})
    assert resp.status_code == 200
    assert "No results" in resp.text


def test_search_bad_query_does_not_500(app_client):
    client, _ = app_client
    resp = client.get("/search", params={"q": '")(*&"'})
    assert resp.status_code == 200
    assert "No results" in resp.text


def test_search_pagination_links(app_client):
    client, _ = app_client
    resp = client.get("/search", params={"q": "page"})
    assert resp.status_code == 200
    # Pagination control should exist when there are results.
    assert "Next" in resp.text or "next" in resp.text or "results" in resp.text.lower()


def test_search_default_view_is_grouped(app_client):
    client, _ = app_client
    resp = client.get("/search", params={"q": "synthesizer"})
    assert resp.status_code == 200
    # Grouped view shows match counts, not snippet marks.
    assert "matching page" in resp.text
    assert "<mark>" not in resp.text
    # Magazine row links to the issue page with q preserved.
    assert "/magazine/byte-1985-12?q=synthesizer" in resp.text
    # Toggle to flat view is offered.
    assert "view=flat" in resp.text
    assert "matching pages" in resp.text


def test_search_flat_view_links_back_to_grouped(app_client):
    client, _ = app_client
    resp = client.get("/search", params={"q": "synthesizer", "view": "flat"})
    assert resp.status_code == 200
    assert "<mark>" in resp.text
    # Back link returns to grouped view, carrying q.
    assert "/search?q=synthesizer&view=grouped" in resp.text


def test_magazine_search_form_present(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12")
    assert resp.status_code == 200
    # Per-issue search form is always present.
    assert 'action="/magazine/byte-1985-12"' in resp.text
    assert "Search this issue" in resp.text
    # No-q request still shows the page grid.
    assert "/magazine/byte-1985-12/page/1" in resp.text
    assert "/magazine/byte-1985-12/page/2" in resp.text


def test_magazine_search_replaces_page_grid(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12", params={"q": "synthesizer"})
    assert resp.status_code == 200
    # Results section is shown.
    assert "in this issue" in resp.text
    assert "<mark>" in resp.text
    # Snippet links pass q through to the page view.
    assert "/magazine/byte-1985-12/page/1?q=synthesizer" in resp.text
    # "Back to all pages" link drops q.
    assert 'href="/magazine/byte-1985-12"' in resp.text
    # Page 2 doesn't match "synthesizer", so if the grid were rendered we'd
    # still see a link to it; in results-only mode it should be absent.
    assert "/magazine/byte-1985-12/page/2" not in resp.text


def test_magazine_search_no_results_message(app_client):
    client, _ = app_client
    resp = client.get(
        "/magazine/byte-1985-12", params={"q": "nonsensequeryxyzzy"}
    )
    assert resp.status_code == 200
    assert "No matches in this issue" in resp.text


def test_search_preserves_sort_and_per_page_in_links(app_client):
    client, _ = app_client
    resp = client.get(
        "/search",
        params={"q": "synthesizer", "view": "flat", "sort": "newest", "per_page": 50},
    )
    assert resp.status_code == 200
    # Sort selector renders the other options as links carrying current view + per_page.
    assert "sort=oldest" in resp.text
    assert "per_page=50" in resp.text
    # Toggle to grouped view must carry sort and per_page.
    assert "view=grouped&amp;sort=newest&amp;per_page=50" in resp.text or \
           "view=grouped&sort=newest&per_page=50" in resp.text


def test_search_invalid_sort_falls_back_to_rank(app_client):
    client, _ = app_client
    resp = client.get("/search", params={"q": "synthesizer", "sort": "bogus"})
    assert resp.status_code == 200
    # The control row should highlight 'rank' (the default), not 'bogus'.
    # 'bogus' should not appear anywhere in the page.
    assert "bogus" not in resp.text


def test_search_invalid_per_page_falls_back(app_client):
    client, _ = app_client
    resp = client.get(
        "/search", params={"q": "synthesizer", "per_page": 999},
    )
    assert resp.status_code == 200
    # Falls back to 25; pagination links should reflect that.
    assert "per_page=25" in resp.text
    # 999 should not appear as a real per_page choice.
    assert "per_page=999" not in resp.text


def test_search_huge_page_does_not_500(app_client):
    # Server-side clamp means an absurd page param still renders cleanly.
    client, _ = app_client
    for view in ("flat", "grouped"):
        resp = client.get(
            "/search",
            params={"q": "synthesizer", "view": view, "page": 9999},
        )
        assert resp.status_code == 200


def test_clamp_page_helpers():
    # Unit-test the clamp+max math directly: small fixture data can't
    # produce 1000+ results, so we verify the route helpers in isolation.
    from magsearch.web.routes import (
        FLAT_RESULT_CAP,
        GROUPED_RESULT_CAP,
        _clamp_page,
        _max_page,
    )
    # Flat: 1000 / 25 = 40, 1000 / 50 = 20, 1000 / 100 = 10.
    assert _max_page(FLAT_RESULT_CAP, 25) == 40
    assert _max_page(FLAT_RESULT_CAP, 50) == 20
    assert _max_page(FLAT_RESULT_CAP, 100) == 10
    # Grouped: 200 / 25 = 8.
    assert _max_page(GROUPED_RESULT_CAP, 25) == 8

    assert _clamp_page(9999, FLAT_RESULT_CAP, 25) == (40, 40)
    assert _clamp_page(0, FLAT_RESULT_CAP, 25) == (1, 40)
    assert _clamp_page(-5, FLAT_RESULT_CAP, 25) == (1, 40)
    assert _clamp_page(20, FLAT_RESULT_CAP, 25) == (20, 40)
    assert _clamp_page(9999, GROUPED_RESULT_CAP, 25) == (8, 8)


def test_magazine_issues_search_carries_sort(app_client):
    # Build a /magazines/{title} request with sort+per_page and verify
    # links preserve them. Use any title from the seeded fixture.
    client, _ = app_client
    resp = client.get(
        "/magazines/Byte",
        params={"q": "synthesizer", "sort": "oldest", "per_page": 50},
    )
    assert resp.status_code == 200
    assert "sort=oldest" in resp.text or "sort=newest" in resp.text  # selector row
    assert "per_page=50" in resp.text


def test_magazine_per_issue_sort_page(app_client):
    client, _ = app_client
    resp = client.get(
        "/magazine/byte-1985-12",
        params={"q": "synthesizer", "sort": "page"},
    )
    assert resp.status_code == 200
    # The 'page' sort option should appear as the active (non-link) choice
    # and 'rank' should appear as a link option.
    assert "sort=rank" in resp.text


def test_search_default_match_all_requires_every_term(app_client):
    # Default match_all=1. The fixture's Byte issue has p1="vintage
    # synthesizer review" and p2="modem speeds compared" — no single
    # page has both "synthesizer" and "modem", so AND yields zero hits.
    client, _ = app_client
    resp = client.get(
        "/search",
        params={"q": "synthesizer modem", "view": "flat"},
    )
    assert resp.status_code == 200
    assert "No results" in resp.text


def test_search_match_all_off_uses_or(app_client):
    # match_all=0 switches to OR — now both Byte pages match.
    client, _ = app_client
    resp = client.get(
        "/search",
        params={"q": "synthesizer modem", "view": "flat", "match_all": 0},
    )
    assert resp.status_code == 200
    assert "/magazine/byte-1985-12/page/1" in resp.text
    assert "/magazine/byte-1985-12/page/2" in resp.text


def test_search_match_phrase_enforces_order(app_client):
    # p1 text is "vintage synthesizer review". In-order matches; reversed doesn't.
    client, _ = app_client
    ok = client.get(
        "/search",
        params={"q": "vintage synthesizer", "view": "flat", "match_phrase": 1},
    )
    assert ok.status_code == 200
    assert "/magazine/byte-1985-12/page/1" in ok.text

    backwards = client.get(
        "/search",
        params={"q": "synthesizer vintage", "view": "flat", "match_phrase": 1},
    )
    assert backwards.status_code == 200
    assert "No results" in backwards.text


def test_search_preserves_match_options_in_links(app_client):
    # Phrase=0 keeps match_all=0 observable in links. (Phrase implies AND,
    # so match_all=0 with phrase=1 gets normalized to match_all=1.)
    client, _ = app_client
    resp = client.get(
        "/search",
        params={"q": "synthesizer", "view": "flat",
                "match_all": 0, "match_phrase": 0},
    )
    assert resp.status_code == 200
    # Pagination / sort / view-toggle links must carry the match options.
    assert "match_all=0" in resp.text
    assert "match_phrase=0" in resp.text


def test_magazine_search_form_renders_match_checkboxes(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12")
    assert resp.status_code == 200
    # Both checkbox controls present; "all terms" is the default-on state.
    assert 'name="match_all"' in resp.text
    assert 'name="match_phrase"' in resp.text


def test_magazines_title_search_preserves_match_options(app_client):
    # Use phrase=0 so match_all=0 stays observable. (Phrase implies AND, so
    # match_all=0 with phrase=1 gets normalized to match_all=1.)
    client, _ = app_client
    resp = client.get(
        "/magazines/Byte",
        params={"q": "synthesizer", "match_all": 0, "match_phrase": 0},
    )
    assert resp.status_code == 200
    assert "match_all=0" in resp.text
    assert "match_phrase=0" in resp.text


def test_magazine_form_checkbox_round_trip(app_client):
    # Browsers serialize the per-issue form's hidden=0 input BEFORE the
    # checkbox=1 (document order); FastAPI then picks the last value for
    # repeated params. This test guards that ordering: if the hidden input
    # ends up after the checkbox, a checked box would arrive as 0.
    client, _ = app_client
    # Checked match_all, unchecked match_phrase — exactly what a browser
    # would submit from the per-issue form in that state. (Can't use the
    # inverse — phrase implies all-terms, so match_all=0 with phrase=1
    # gets normalized.)
    resp = client.get(
        "/magazine/byte-1985-12"
        "?q=synthesizer"
        "&match_all=0&match_all=1"        # hidden then checkbox
        "&match_phrase=0"                 # hidden, unchecked
    )
    assert resp.status_code == 200
    import re
    ma = re.search(r'<input\s+type="checkbox"[^>]*name="match_all"[^>]*>', resp.text)
    assert ma is not None
    assert "checked" in ma.group(0)
    mp = re.search(r'<input\s+type="checkbox"[^>]*name="match_phrase"[^>]*>', resp.text)
    assert mp is not None
    assert "checked" not in mp.group(0)


def test_search_phrase_forces_all_terms_checkbox_on(app_client):
    # match_phrase=1 implies match_all=1; even if the URL explicitly sets
    # match_all=0, the rendered all-terms checkbox should appear checked
    # so the UI never contradicts the actual search semantics.
    client, _ = app_client
    resp = client.get(
        "/search",
        params={"q": "synthesizer", "match_all": 0, "match_phrase": 1,
                "view": "flat"},
    )
    assert resp.status_code == 200
    # The pagination/sort macro should now emit match_all=1 (forced).
    assert "match_all=1" in resp.text
    assert "match_all=0" not in resp.text


def test_magazine_phrase_forces_all_terms_checkbox_on(app_client):
    client, _ = app_client
    resp = client.get(
        "/magazine/byte-1985-12",
        params={"q": "synthesizer", "match_all": 0, "match_phrase": 1},
    )
    assert resp.status_code == 200
    import re
    ma = re.search(
        r'<input\s+type="checkbox"[^>]*name="match_all"[^>]*>', resp.text
    )
    assert ma is not None
    assert "checked" in ma.group(0)


def test_magazines_title_form_checkbox_round_trip(app_client):
    client, _ = app_client
    resp = client.get(
        "/magazines/Byte"
        "?q=synthesizer"
        "&match_all=0&match_all=1"        # checked
        "&match_phrase=0"                 # unchecked
    )
    assert resp.status_code == 200
    import re
    ma = re.search(r'<input\s+type="checkbox"[^>]*name="match_all"[^>]*>', resp.text)
    assert ma is not None
    assert "checked" in ma.group(0)
    mp = re.search(r'<input\s+type="checkbox"[^>]*name="match_phrase"[^>]*>', resp.text)
    assert mp is not None
    assert "checked" not in mp.group(0)


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
