from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.settings import get_settings
from magsearch.web.config_service import set_value
from tests.conftest import create_user, login


def _factory():
    settings = get_settings()
    return make_session_factory(make_engine(settings.database_url))


def _set_require_login(value: bool) -> None:
    with session_scope(_factory()) as s:
        set_value(s, "require_login", value)


def test_default_anonymous_can_browse(app_client):
    client, _ = app_client
    assert client.get("/").status_code == 200
    assert client.get("/magazines").status_code == 200


def test_with_require_login_anonymous_redirects_to_login(app_client):
    client, _ = app_client
    _set_require_login(True)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login?next=%2F" in resp.headers["location"]


def test_login_page_always_reachable(app_client):
    client, _ = app_client
    _set_require_login(True)
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 200


def test_logged_in_user_passes_gate(app_client):
    client, _ = app_client
    create_user(_factory(), "bob", "bob-pw", is_admin=False)
    _set_require_login(True)
    login(client, "bob", "bob-pw")
    assert client.get("/").status_code == 200
    assert client.get("/magazines").status_code == 200


def test_gate_takes_effect_immediately(app_client):
    """Toggling require_login should affect the very next request, no restart."""
    client, _ = app_client
    assert client.get("/").status_code == 200
    _set_require_login(True)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    _set_require_login(False)
    assert client.get("/").status_code == 200


def test_query_string_preserved_in_next(app_client):
    client, _ = app_client
    _set_require_login(True)
    resp = client.get("/search?q=foo", follow_redirects=False)
    assert resp.status_code == 303
    assert "%2Fsearch%3Fq%3Dfoo" in resp.headers["location"]
