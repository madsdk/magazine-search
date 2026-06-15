from tests.conftest import create_user, login
from magsearch.db import make_engine, make_session_factory
from magsearch.settings import get_settings


def _factory():
    settings = get_settings()
    return make_session_factory(make_engine(settings.database_url))


def _login_bob(client):
    create_user(_factory(), "bob", "bob-pw", is_admin=False)
    login(client, "bob", "bob-pw")


def test_pages_json_requires_login(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/pages.json", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_pages_json_returns_ordered_pages(app_client):
    client, _ = app_client
    _login_bob(client)
    resp = client.get("/magazine/byte-1985-12/pages.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["page_count"] == 2
    assert body["pages"] == [
        {"n": 1, "image_path": "byte-1985-12/pages/0001.webp"},
        {"n": 2, "image_path": "byte-1985-12/pages/0002.webp"},
    ]


def test_pages_json_404_for_unknown_magazine(app_client):
    client, _ = app_client
    _login_bob(client)
    resp = client.get("/magazine/does-not-exist/pages.json")
    assert resp.status_code == 404
