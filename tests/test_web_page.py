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
