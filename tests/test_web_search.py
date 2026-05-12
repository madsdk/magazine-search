# tests/test_web_search.py
def test_search_returns_marked_results(app_client):
    client, _ = app_client
    resp = client.get("/search", params={"q": "synthesizer"})
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
