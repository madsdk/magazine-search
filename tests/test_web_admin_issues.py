from sqlalchemy import select

from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.models import Magazine, Page
from magsearch.settings import get_settings
from tests.conftest import get_csrf


def _factory():
    settings = get_settings()
    return make_session_factory(make_engine(settings.database_url))


def test_issues_index_lists_magazines(admin_client):
    client, _ = admin_client
    resp = client.get("/admin/issues")
    assert resp.status_code == 200
    assert "Byte" in resp.text
    assert "Compute" in resp.text


def test_issue_edit_updates_metadata(admin_client):
    client, _ = admin_client
    token = get_csrf(client)
    resp = client.post(
        "/admin/issues/byte-1985-12/edit",
        data={
            "title": "Byte Magazine",
            "issue": "12",
            "publication_date": "1985-12-15",
            "publisher": "McGraw-Hill Publishing",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with session_scope(_factory()) as s:
        mag = s.get(Magazine, "byte-1985-12")
        assert mag.title == "Byte Magazine"
        assert mag.issue == "12"
        assert mag.publisher == "McGraw-Hill Publishing"
        assert str(mag.publication_date) == "1985-12-15"


def test_issue_delete_removes_db_pages_and_bundle(admin_client):
    client, bundles = admin_client
    bundle_dir = bundles / "byte-1985-12"
    assert bundle_dir.exists()

    token = get_csrf(client)
    resp = client.post(
        "/admin/issues/byte-1985-12/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with session_scope(_factory()) as s:
        assert s.get(Magazine, "byte-1985-12") is None
        # Page rows for byte-1985-12 are cascaded away.
        rows = s.scalars(select(Page).where(Page.magazine_id == "byte-1985-12")).all()
        assert rows == []

    assert not bundle_dir.exists(), "bundle directory should be removed from disk"


def test_issue_edit_rejects_without_csrf(admin_client):
    client, _ = admin_client
    resp = client.post(
        "/admin/issues/byte-1985-12/edit",
        data={"title": "Hacked", "issue": "", "publication_date": "", "publisher": ""},
        follow_redirects=False,
    )
    # require_csrf raises 403 (or 422 if Form field missing — both are non-success)
    assert resp.status_code in (403, 422)


def test_issue_edit_invalid_date_returns_400(admin_client):
    client, _ = admin_client
    token = get_csrf(client)
    resp = client.post(
        "/admin/issues/byte-1985-12/edit",
        data={
            "title": "Byte",
            "issue": "",
            "publication_date": "not-a-date",
            "publisher": "",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400
