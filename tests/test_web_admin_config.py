from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.settings import get_settings
from magsearch.web.config_service import get_value, set_value
from tests.conftest import get_csrf


def _factory():
    settings = get_settings()
    return make_session_factory(make_engine(settings.database_url))


def test_config_page_renders(admin_client):
    client, _ = admin_client
    resp = client.get("/admin/config")
    assert resp.status_code == 200
    assert "require_login" in resp.text


def test_config_toggle_persists(admin_client):
    client, _ = admin_client
    token = get_csrf(client)

    # Enable
    resp = client.post(
        "/admin/config",
        data={"require_login": "1", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope(_factory()) as s:
        assert get_value(s, "require_login") is True

    # Disable (checkbox omitted -> false)
    token = get_csrf(client)
    resp = client.post(
        "/admin/config",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope(_factory()) as s:
        assert get_value(s, "require_login") is False


def test_config_service_coercion(app_client):
    """Round-trip values through the service."""
    factory = _factory()
    with session_scope(factory) as s:
        set_value(s, "require_login", True)
    with session_scope(factory) as s:
        assert get_value(s, "require_login") is True
    with session_scope(factory) as s:
        set_value(s, "require_login", False)
    with session_scope(factory) as s:
        assert get_value(s, "require_login") is False
