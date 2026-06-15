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


def test_reader_requires_login(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/read/1", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_reader_renders_for_logged_in_user(app_client):
    client, _ = app_client
    _login_bob(client)
    resp = client.get("/magazine/byte-1985-12/read/1")
    assert resp.status_code == 200
    # Seeds config the JS reads.
    assert '"magazine_id": "byte-1985-12"' in resp.text
    assert '"start_page": 1' in resp.text
    assert '"page_count": 2' in resp.text
    # References the page-list endpoint and the static module.
    assert "/magazine/byte-1985-12/pages.json" in resp.text
    assert "/static/reader.js" in resp.text
    # Standalone document: no site masthead.
    assert "The Archive" not in resp.text


def test_reader_clamps_out_of_range_page(app_client):
    client, _ = app_client
    _login_bob(client)
    # Page 999 of a 2-page issue clamps to the last page.
    resp = client.get("/magazine/byte-1985-12/read/999")
    assert resp.status_code == 200
    assert '"start_page": 2' in resp.text
    # Page 0 clamps up to 1.
    resp = client.get("/magazine/byte-1985-12/read/0")
    assert resp.status_code == 200
    assert '"start_page": 1' in resp.text


def test_reader_404_for_unknown_magazine(app_client):
    client, _ = app_client
    _login_bob(client)
    resp = client.get("/magazine/does-not-exist/read/1")
    assert resp.status_code == 404


def test_reader_carries_query(app_client):
    client, _ = app_client
    _login_bob(client)
    resp = client.get("/magazine/byte-1985-12/read/1?q=synthesizer")
    assert resp.status_code == 200
    assert '"q": "synthesizer"' in resp.text


def test_page_viewer_has_reader_button(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/page/2")
    assert resp.status_code == 200
    assert "/magazine/byte-1985-12/read/2" in resp.text


def test_page_viewer_reader_button_carries_query(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/page/1?q=synthesizer")
    assert resp.status_code == 200
    assert "/magazine/byte-1985-12/read/1?q=synthesizer" in resp.text
