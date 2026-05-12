def test_bundle_serves_existing_thumb(app_client):
    client, _ = app_client
    resp = client.get("/bundle/byte-1985-12/thumbs/0001.webp")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/")


def test_bundle_404_for_missing(app_client):
    client, _ = app_client
    resp = client.get("/bundle/byte-1985-12/thumbs/9999.webp")
    assert resp.status_code == 404


def test_bundle_rejects_path_traversal_dotdot(app_client):
    client, _ = app_client
    resp = client.get("/bundle/byte-1985-12/../../etc/passwd")
    assert resp.status_code in (400, 404)


def test_bundle_rejects_absolute_path(app_client):
    client, _ = app_client
    resp = client.get("/bundle//etc/passwd")
    # FastAPI may 404 on the double-slash normalization; either way must not 200.
    assert resp.status_code != 200
