from datetime import datetime

from sqlalchemy import select

from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.models import User
from magsearch.settings import get_settings
from magsearch.web.auth import hash_password
from tests.conftest import create_user, get_csrf, login


def _factory():
    settings = get_settings()
    return make_session_factory(make_engine(settings.database_url))


def test_login_form_renders(app_client):
    client, _ = app_client
    resp = client.get("/login")
    assert resp.status_code == 200
    assert 'name="csrf_token"' in resp.text
    assert 'name="username"' in resp.text


def test_login_bad_password_re_renders_with_error(app_client):
    client, _ = app_client
    create_user(_factory(), "alice", "good-pw", is_admin=True)

    token = get_csrf(client)
    resp = client.post(
        "/login",
        data={"username": "alice", "password": "wrong", "csrf_token": token, "next": "/"},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert "Invalid" in resp.text


def test_login_good_password_redirects_and_updates_last_login(app_client):
    client, _ = app_client
    create_user(_factory(), "alice", "alice-pw", is_admin=True)

    login(client, "alice", "alice-pw")

    with session_scope(_factory()) as s:
        u = s.scalar(select(User).where(User.username == "alice"))
        assert u is not None
        assert isinstance(u.last_login_at, datetime)


def test_login_respects_next_param(app_client):
    client, _ = app_client
    create_user(_factory(), "alice", "alice-pw", is_admin=True)

    token = get_csrf(client)
    resp = client.post(
        "/login",
        data={"username": "alice", "password": "alice-pw",
              "csrf_token": token, "next": "/admin/users"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/users"


def test_open_redirect_protection(app_client):
    client, _ = app_client
    create_user(_factory(), "alice", "alice-pw", is_admin=True)

    token = get_csrf(client)
    resp = client.post(
        "/login",
        data={"username": "alice", "password": "alice-pw",
              "csrf_token": token, "next": "https://evil.example.com/"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_logout_clears_session(app_client):
    client, _ = app_client
    create_user(_factory(), "alice", "alice-pw", is_admin=True)
    login(client, "alice", "alice-pw")

    token = get_csrf(client)
    resp = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert resp.status_code == 303
    # After logout, /admin should redirect to login.
    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]
