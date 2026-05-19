def test_magazines_lists_all(app_client):
    client, _ = app_client
    resp = client.get("/magazines")
    assert resp.status_code == 200
    assert "Byte" in resp.text
    assert "Compute" in resp.text


def test_magazine_detail_shows_pages(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12")
    assert resp.status_code == 200
    assert "Byte" in resp.text
    # Should link to page 1 and page 2.
    assert "/magazine/byte-1985-12/page/1" in resp.text
    assert "/magazine/byte-1985-12/page/2" in resp.text


def test_magazine_detail_404_for_unknown(app_client):
    client, _ = app_client
    resp = client.get("/magazine/nonexistent")
    assert resp.status_code == 404
