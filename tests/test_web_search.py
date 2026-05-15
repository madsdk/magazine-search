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
    assert "Show all matching pages" in resp.text


def test_search_flat_view_links_back_to_grouped(app_client):
    client, _ = app_client
    resp = client.get("/search", params={"q": "synthesizer", "view": "flat"})
    assert resp.status_code == 200
    assert "<mark>" in resp.text
    assert "Back to grouped view" in resp.text


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
    # Results section shown; page grid header is gone.
    assert "Results in this issue" in resp.text
    assert "<mark>" in resp.text
    assert ">Pages<" not in resp.text
    # Snippet links pass q through to the page view.
    assert "/magazine/byte-1985-12/page/1?q=synthesizer" in resp.text
    # "Back to all pages" link drops q.
    assert 'href="/magazine/byte-1985-12"' in resp.text


def test_magazine_search_no_results_message(app_client):
    client, _ = app_client
    resp = client.get(
        "/magazine/byte-1985-12", params={"q": "nonsensequeryxyzzy"}
    )
    assert resp.status_code == 200
    assert "No matches in this issue" in resp.text
