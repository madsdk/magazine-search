def test_page_view_shows_image(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/page/1")
    assert resp.status_code == 200
    assert "/bundle/byte-1985-12/pages/0001.webp" in resp.text


def test_page_view_prev_next_links(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/page/1")
    assert "/magazine/byte-1985-12/page/2" in resp.text
    # No "previous" on page 1
    assert "/magazine/byte-1985-12/page/0" not in resp.text

    resp = client.get("/magazine/byte-1985-12/page/2")
    assert "/magazine/byte-1985-12/page/1" in resp.text
    assert "/magazine/byte-1985-12/page/3" not in resp.text  # only 2 pages


def test_page_view_404_for_bad_page(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/page/999")
    assert resp.status_code == 404


def test_page_view_preserves_query_in_back_link(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/page/1?q=synthesizer")
    assert "synthesizer" in resp.text  # back link or search bar echoes it


def test_page_view_emits_arrow_key_nav_script(app_client):
    # Page 1 of a 2-page issue: next exists, prev does not.
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/page/1")
    assert resp.status_code == 200
    assert "ArrowLeft" in resp.text and "ArrowRight" in resp.text
    # JSON-encoded next URL must include the target page.
    assert '"/magazine/byte-1985-12/page/2"' in resp.text
    # No prev on page 1 — prev is null.
    assert "var prev = null" in resp.text

    # Page 2 of the same issue: prev exists, next does not.
    resp = client.get("/magazine/byte-1985-12/page/2")
    assert resp.status_code == 200
    assert '"/magazine/byte-1985-12/page/1"' in resp.text
    assert "var next = null" in resp.text


def test_page_view_arrow_nav_carries_query(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/page/1?q=synthesizer")
    assert resp.status_code == 200
    # The JS-embedded next URL should carry q for highlight continuity.
    assert '"/magazine/byte-1985-12/page/2?q=synthesizer"' in resp.text
