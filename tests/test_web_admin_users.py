from sqlalchemy import select

from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.models import User
from magsearch.settings import get_settings
from magsearch.web.auth import verify_password
from tests.conftest import create_user, get_csrf


def _factory():
    settings = get_settings()
    return make_session_factory(make_engine(settings.database_url))


def test_users_index_lists_admin(admin_client):
    client, _ = admin_client
    resp = client.get("/admin/users")
    assert resp.status_code == 200
    assert "alice" in resp.text


def test_create_new_user(admin_client):
    client, _ = admin_client
    token = get_csrf(client)
    resp = client.post(
        "/admin/users/new",
        data={"username": "carol", "password": "carol-pw",
              "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with session_scope(_factory()) as s:
        u = s.scalar(select(User).where(User.username == "carol"))
        assert u is not None
        assert u.is_admin is False
        assert verify_password("carol-pw", u.password_hash)


def test_create_user_duplicate_rejected(admin_client):
    client, _ = admin_client
    token = get_csrf(client)
    client.post(
        "/admin/users/new",
        data={"username": "carol", "password": "carol-pw", "csrf_token": token},
        follow_redirects=False,
    )
    token = get_csrf(client)
    resp = client.post(
        "/admin/users/new",
        data={"username": "carol", "password": "other-pw", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_edit_user_resets_password_and_promotes(admin_client):
    client, _ = admin_client
    uid = create_user(_factory(), "carol", "carol-pw", is_admin=False)

    token = get_csrf(client)
    resp = client.post(
        f"/admin/users/{uid}/edit",
        data={"username": "carol", "password": "new-pw",
              "is_admin": "1", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with session_scope(_factory()) as s:
        u = s.get(User, uid)
        assert u.is_admin is True
        assert verify_password("new-pw", u.password_hash)


def test_cannot_delete_last_admin(admin_client):
    """alice is the only admin; deleting her must be blocked."""
    client, _ = admin_client
    with session_scope(_factory()) as s:
        alice = s.scalar(select(User).where(User.username == "alice"))
        alice_id = alice.id

    token = get_csrf(client)
    resp = client.post(
        f"/admin/users/{alice_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    # Either "cannot delete yourself" (same session) or "last admin".
    assert resp.status_code == 400

    with session_scope(_factory()) as s:
        assert s.scalar(select(User).where(User.username == "alice")) is not None


def test_cannot_demote_last_admin(admin_client):
    client, _ = admin_client
    with session_scope(_factory()) as s:
        alice = s.scalar(select(User).where(User.username == "alice"))
        alice_id = alice.id

    token = get_csrf(client)
    resp = client.post(
        f"/admin/users/{alice_id}/edit",
        data={"username": "alice", "password": "",
              "csrf_token": token},  # is_admin omitted -> false
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_delete_other_user(admin_client):
    client, _ = admin_client
    bob_id = create_user(_factory(), "bob", "bob-pw", is_admin=False)

    token = get_csrf(client)
    resp = client.post(
        f"/admin/users/{bob_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with session_scope(_factory()) as s:
        assert s.get(User, bob_id) is None
