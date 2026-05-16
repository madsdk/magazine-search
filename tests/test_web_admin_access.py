def test_anonymous_admin_redirects_to_login(app_client):
    client, _ = app_client
    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_non_admin_user_gets_403(user_client):
    client, _ = user_client
    resp = client.get("/admin", follow_redirects=False)
    assert resp.status_code == 403


def test_admin_user_sees_dashboard(admin_client):
    client, _ = admin_client
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "Administration" in resp.text
    assert "Issues" in resp.text
    assert "Users" in resp.text
