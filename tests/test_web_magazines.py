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


def test_magazine_detail_shows_download_link_in_web_mode(app_client):
    # The download link is a multi-user feature; in desktop mode the file is
    # already on the user's disk so the link is hidden (see desktop test).
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12")
    assert resp.status_code == 200
    assert "download original" in resp.text
    assert "/bundle/byte-1985-12/original." in resp.text


def test_magazine_detail_hides_download_link_in_desktop_mode(desktop_app_client):
    client, _ = desktop_app_client
    resp = client.get("/magazine/byte-1985-12")
    assert resp.status_code == 200
    assert "download original" not in resp.text
    assert "/bundle/byte-1985-12/original." not in resp.text
