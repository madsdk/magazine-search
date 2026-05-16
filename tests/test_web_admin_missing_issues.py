from datetime import date, datetime

from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.models import Magazine
from magsearch.settings import get_settings


def _factory():
    settings = get_settings()
    return make_session_factory(make_engine(settings.database_url))


def _seed_issues(title: str, issue_values: list[str | None]) -> None:
    """Insert minimal Magazine rows for `title`, one per entry in issue_values."""
    with session_scope(_factory()) as s:
        for i, raw in enumerate(issue_values):
            s.add(Magazine(
                id=f"{title.lower()}-fixture-{i}",
                title=title,
                issue=raw,
                publication_date=date(2000, 1, 1),
                publisher=None,
                original_filename=f"{title}-{i}.pdf",
                original_format="pdf",
                page_count=1,
                content_hash=f"hash-{i}",
                cover_path=f"{title}/cover-{i}.jpg",
                ocr_engine="fake",
                ocr_engine_version="0",
                ingested_at=datetime.utcnow(),
            ))


def test_missing_issues_requires_login(app_client):
    client, _ = app_client
    resp = client.get(
        "/admin/magazines/Anything/missing-issues", follow_redirects=False
    )
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_missing_issues_forbidden_for_non_admin(user_client):
    client, _ = user_client
    resp = client.get(
        "/admin/magazines/Anything/missing-issues", follow_redirects=False
    )
    assert resp.status_code == 403


def test_missing_issues_404_when_title_unknown(admin_client):
    client, _ = admin_client
    resp = client.get("/admin/magazines/NoSuchTitle/missing-issues")
    assert resp.status_code == 404


def test_missing_issues_lists_gaps(admin_client):
    client, _ = admin_client
    _seed_issues("Gappy", ["1", "2", "4", "7"])
    resp = client.get("/admin/magazines/Gappy/missing-issues")
    assert resp.status_code == 200
    body = resp.text
    for n in ("№ 3", "№ 5", "№ 6"):
        assert n in body, f"expected {n!r} in response"
    # Anything above the highest existing (7) should NOT be listed as missing.
    assert "№ 8" not in body
    assert "highest" in body and "7" in body


def test_missing_issues_no_gaps_message(admin_client):
    client, _ = admin_client
    _seed_issues("Solid", ["1", "2", "3"])
    resp = client.get("/admin/magazines/Solid/missing-issues")
    assert resp.status_code == 200
    assert "No gaps" in resp.text


def test_missing_issues_excluded_section_lists_non_numeric(admin_client):
    client, _ = admin_client
    _seed_issues("Mixed", ["2", "3", "Special", None])
    resp = client.get("/admin/magazines/Mixed/missing-issues")
    assert resp.status_code == 200
    body = resp.text
    # Gap math runs over the two numeric rows only -> missing is [1].
    assert "№ 1" in body
    assert "highest" in body and "3" in body
    # Excluded rows are surfaced with edit links to the existing admin form.
    assert "Not counted" in body
    assert "Special" in body
    assert "/admin/issues/mixed-fixture-2/edit" in body
    assert "/admin/issues/mixed-fixture-3/edit" in body


def test_missing_issues_when_no_numeric_values(admin_client):
    client, _ = admin_client
    _seed_issues("Stringly", ["Special", None, "12A"])
    resp = client.get("/admin/magazines/Stringly/missing-issues")
    assert resp.status_code == 200
    body = resp.text
    assert "Cannot compute gaps" in body
    assert "Not counted" in body
    # All three rows listed in the excluded table.
    assert "Special" in body
    assert "12A" in body


def test_missing_issues_button_visible_to_admin(admin_client):
    client, _ = admin_client
    # The conftest seeds a single "Byte" issue, so /magazines/Byte is reachable.
    resp = client.get("/magazines/Byte")
    assert resp.status_code == 200
    assert "/admin/magazines/Byte/missing-issues" in resp.text


def test_missing_issues_button_hidden_for_anonymous(app_client):
    client, _ = app_client
    resp = client.get("/magazines/Byte")
    assert resp.status_code == 200
    assert "/admin/magazines/Byte/missing-issues" not in resp.text


def test_missing_issues_button_hidden_for_non_admin(user_client):
    client, _ = user_client
    resp = client.get("/magazines/Byte")
    assert resp.status_code == 200
    assert "/admin/magazines/Byte/missing-issues" not in resp.text
