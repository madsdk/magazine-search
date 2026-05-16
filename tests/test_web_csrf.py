from tests.conftest import get_csrf


def test_login_post_without_csrf_rejected(app_client):
    client, _ = app_client
    resp = client.post(
        "/login",
        data={"username": "alice", "password": "alice-pw", "next": "/"},
        follow_redirects=False,
    )
    # Missing csrf_token -> require_csrf raises 403 (Form field missing -> 422 fallback).
    assert resp.status_code in (403, 422)


def test_admin_post_with_wrong_csrf_rejected(admin_client):
    client, _ = admin_client
    resp = client.post(
        "/admin/issues/byte-1985-12/edit",
        data={"title": "X", "issue": "", "publication_date": "",
              "publisher": "", "csrf_token": "definitely-not-the-token"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_admin_post_with_correct_csrf_accepted(admin_client):
    client, _ = admin_client
    token = get_csrf(client)
    resp = client.post(
        "/admin/issues/byte-1985-12/edit",
        data={"title": "Byte", "issue": "", "publication_date": "",
              "publisher": "", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
