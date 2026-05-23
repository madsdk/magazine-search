from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.models import (
    Magazine,
    Page,
    ResearchTopic,
    ResearchTopicPage,
    User,
)
from magsearch.settings import get_settings
from tests.conftest import create_user, get_csrf, login


def _factory():
    settings = get_settings()
    return make_session_factory(make_engine(settings.database_url))


def _create_topic(user_id: int, name: str, description: str | None = None) -> int:
    """Insert a topic directly. Returns its id."""
    now = datetime.utcnow()
    with session_scope(_factory()) as s:
        t = ResearchTopic(
            user_id=user_id,
            name=name,
            description=description,
            created_at=now,
            updated_at=now,
        )
        s.add(t)
        s.flush()
        return t.id


def _page_id(magazine_id: str, page_number: int) -> int:
    with session_scope(_factory()) as s:
        p = s.scalar(
            select(Page).where(
                Page.magazine_id == magazine_id, Page.page_number == page_number
            )
        )
        assert p is not None
        return p.id


# ---------- Auth gating ----------

def test_anonymous_redirected_from_research_index(app_client):
    client, _ = app_client
    resp = client.get("/research", follow_redirects=False)
    # require_user issues a 303 with Location header on missing user.
    assert resp.status_code == 303
    assert "/login" in resp.headers.get("location", "")


def test_anonymous_redirected_from_topic_detail(app_client):
    client, _ = app_client
    resp = client.get("/research/1", follow_redirects=False)
    assert resp.status_code == 303
    assert "/login" in resp.headers.get("location", "")


def test_anonymous_redirected_from_save_page(app_client):
    client, _ = app_client
    # No CSRF needed — auth check fires first.
    resp = client.post(
        "/research/save-page",
        data={"page_id": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/login" in resp.headers.get("location", "")


# ---------- Nav + save-trigger visibility ----------

def test_my_research_link_only_for_researchers(app_client):
    client, _ = app_client
    # Anonymous: link absent on a public page.
    resp = client.get("/magazines")
    assert resp.status_code == 200
    assert "/research" not in resp.text

    # Logged-in researcher: link present.
    create_user(_factory(), "carol", "carol-pw", is_researcher=True)
    login(client, "carol", "carol-pw")
    resp = client.get("/magazines")
    assert resp.status_code == 200
    assert "/research" in resp.text


def test_save_to_research_trigger_only_for_researchers(app_client):
    # The trigger is an icon-only button; match by element id (the
    # modal id also appears in an unrelated keyboard-shortcut JS
    # handler regardless of role, so we match the element attribute
    # form, not the bare id string).
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/page/1")
    assert resp.status_code == 200
    assert 'id="save-to-research-btn"' not in resp.text
    assert 'id="save-to-research-modal"' not in resp.text

    create_user(_factory(), "carol", "carol-pw", is_researcher=True)
    login(client, "carol", "carol-pw")
    resp = client.get("/magazine/byte-1985-12/page/1")
    assert resp.status_code == 200
    assert 'id="save-to-research-btn"' in resp.text
    assert 'id="save-to-research-modal"' in resp.text


def test_page_view_skips_research_data_for_non_researcher(app_client):
    """A logged-in non-researcher user must not have their (or anyone's)
    topic data loaded into the page-view template context — the UI hides
    research controls for them, so the queries are pure waste and the
    topic names should not leak into the rendered HTML."""
    client, _ = app_client
    # Seed a user with topics, but leave is_researcher=False.
    user_id = create_user(_factory(), "carol", "carol-pw")
    _create_topic(user_id, "carols-secret-topic-name")
    login(client, "carol", "carol-pw")

    resp = client.get("/magazine/byte-1985-12/page/1")
    assert resp.status_code == 200
    assert "carols-secret-topic-name" not in resp.text
    assert 'id="save-to-research-modal"' not in resp.text


def test_research_routes_404_in_desktop_mode(desktop_app_client):
    client, _ = desktop_app_client
    # In desktop mode the router is not registered, so /research is a 404
    # not a redirect.
    resp = client.get("/research", follow_redirects=False)
    assert resp.status_code == 404


def test_save_to_research_trigger_absent_in_desktop_mode(desktop_app_client):
    client, _ = desktop_app_client
    resp = client.get("/magazine/byte-1985-12/page/1")
    assert resp.status_code == 200
    assert "save to research" not in resp.text


# ---------- Topic CRUD ----------

def test_user_can_create_topic(user_client):
    client, _ = user_client
    token = get_csrf(client)
    resp = client.post(
        "/research",
        data={
            "name": "8-bit microcomputers",
            "description": "early home machines",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope(_factory()) as s:
        t = s.scalar(select(ResearchTopic).where(
            ResearchTopic.name == "8-bit microcomputers"
        ))
        assert t is not None
        assert t.description == "early home machines"


def test_topic_visible_only_to_owner(user_client):
    client, _ = user_client
    # Create a topic as bob.
    with session_scope(_factory()) as s:
        bob = s.scalar(select(User).where(User.username == "bob"))
        bob_id = bob.id
    tid = _create_topic(bob_id, "bob's topic")

    # bob sees it.
    resp = client.get(f"/research/{tid}")
    assert resp.status_code == 200
    assert "bob&#39;s topic" in resp.text or "bob's topic" in resp.text

    # bob's index lists it.
    resp = client.get("/research")
    assert resp.status_code == 200
    assert "bob" in resp.text  # username in header
    assert "bob&#39;s topic" in resp.text or "bob's topic" in resp.text

    # carol logs in (different client) and gets 404 on bob's topic.
    # She has the researcher role too — the assertion under test is the
    # per-topic ownership check, not the role gate.
    create_user(_factory(), "carol", "carol-pw", is_researcher=True)
    from fastapi.testclient import TestClient
    from magsearch.web.app import create_app
    other = TestClient(create_app())
    login(other, "carol", "carol-pw")
    resp = other.get(f"/research/{tid}", follow_redirects=False)
    assert resp.status_code == 404


def test_topic_edit_updates_name_and_description(user_client):
    client, _ = user_client
    with session_scope(_factory()) as s:
        bob = s.scalar(select(User).where(User.username == "bob"))
        tid = _create_topic(bob.id, "old name", "old desc")

    token = get_csrf(client)
    resp = client.post(
        f"/research/{tid}/edit",
        data={"name": "new name", "description": "new desc", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with session_scope(_factory()) as s:
        t = s.get(ResearchTopic, tid)
        assert t.name == "new name"
        assert t.description == "new desc"


def test_topic_delete_removes_topic(user_client):
    client, _ = user_client
    with session_scope(_factory()) as s:
        bob = s.scalar(select(User).where(User.username == "bob"))
        tid = _create_topic(bob.id, "ephemeral")

    token = get_csrf(client)
    resp = client.post(
        f"/research/{tid}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope(_factory()) as s:
        assert s.get(ResearchTopic, tid) is None


def test_user_cannot_edit_another_users_topic(user_client):
    client, _ = user_client
    # Create a topic owned by carol; bob (signed in) tries to edit it.
    carol_id = create_user(_factory(), "carol", "carol-pw")
    tid = _create_topic(carol_id, "carol's")
    token = get_csrf(client)
    resp = client.post(
        f"/research/{tid}/edit",
        data={"name": "hacked", "description": "", "csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 404
    with session_scope(_factory()) as s:
        assert s.get(ResearchTopic, tid).name == "carol's"


# ---------- save-page reconciliation ----------

def test_save_page_creates_rows_for_new_topics(user_client):
    client, _ = user_client
    with session_scope(_factory()) as s:
        bob = s.scalar(select(User).where(User.username == "bob"))
        bob_id = bob.id
    t1 = _create_topic(bob_id, "T1")
    t2 = _create_topic(bob_id, "T2")
    page_id = _page_id("byte-1985-12", 1)

    token = get_csrf(client)
    resp = client.post(
        "/research/save-page",
        data={
            "csrf_token": token,
            "page_id": str(page_id),
            "topic_ids": [str(t1), str(t2)],
            "note": "interesting",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope(_factory()) as s:
        rows = s.scalars(
            select(ResearchTopicPage).where(
                ResearchTopicPage.page_id == page_id
            )
        ).all()
        assert {r.topic_id for r in rows} == {t1, t2}
        assert all(r.note == "interesting" for r in rows)


def test_save_page_reconciles_membership(user_client):
    """Prior membership in {t1, t2}; submit {t2, t3} → drops t1, adds t3."""
    client, _ = user_client
    with session_scope(_factory()) as s:
        bob = s.scalar(select(User).where(User.username == "bob"))
        bob_id = bob.id
    t1 = _create_topic(bob_id, "T1")
    t2 = _create_topic(bob_id, "T2")
    t3 = _create_topic(bob_id, "T3")
    page_id = _page_id("byte-1985-12", 1)

    # Seed prior membership.
    now = datetime.utcnow()
    with session_scope(_factory()) as s:
        s.add(ResearchTopicPage(topic_id=t1, page_id=page_id, note="x", saved_at=now))
        s.add(ResearchTopicPage(topic_id=t2, page_id=page_id, note="x", saved_at=now))

    token = get_csrf(client)
    resp = client.post(
        "/research/save-page",
        data={
            "csrf_token": token,
            "page_id": str(page_id),
            "topic_ids": [str(t2), str(t3)],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope(_factory()) as s:
        rows = s.scalars(
            select(ResearchTopicPage).where(
                ResearchTopicPage.page_id == page_id
            )
        ).all()
        assert {r.topic_id for r in rows} == {t2, t3}


def test_save_page_with_new_topic_name_creates_topic(user_client):
    client, _ = user_client
    page_id = _page_id("byte-1985-12", 1)
    token = get_csrf(client)

    resp = client.post(
        "/research/save-page",
        data={
            "csrf_token": token,
            "page_id": str(page_id),
            "new_topic_name": "Fresh idea",
            "note": "found something",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope(_factory()) as s:
        topic = s.scalar(
            select(ResearchTopic).where(ResearchTopic.name == "Fresh idea")
        )
        assert topic is not None
        rows = s.scalars(
            select(ResearchTopicPage).where(
                ResearchTopicPage.topic_id == topic.id
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].page_id == page_id
        assert rows[0].note == "found something"


def test_save_page_rejects_scheme_relative_return_to(user_client):
    """A return_to like //evil.example must not be honored — browsers
    treat it as cross-origin even though it starts with '/'."""
    client, _ = user_client
    page_id = _page_id("byte-1985-12", 1)
    token = get_csrf(client)
    for bad in ("//evil.example", "//evil.example/path", "https://evil.example/"):
        resp = client.post(
            "/research/save-page",
            data={
                "csrf_token": token,
                "page_id": str(page_id),
                "return_to": bad,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/research", bad


def test_save_page_ignores_topic_ids_belonging_to_other_user(user_client):
    """A malicious POST referencing carol's topic id must not add bob's
    page to carol's topic."""
    client, _ = user_client
    carol_id = create_user(_factory(), "carol", "carol-pw")
    carols_topic = _create_topic(carol_id, "carol's secret")
    page_id = _page_id("byte-1985-12", 1)

    token = get_csrf(client)
    resp = client.post(
        "/research/save-page",
        data={
            "csrf_token": token,
            "page_id": str(page_id),
            "topic_ids": [str(carols_topic)],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope(_factory()) as s:
        rows = s.scalars(
            select(ResearchTopicPage).where(
                ResearchTopicPage.topic_id == carols_topic
            )
        ).all()
        assert rows == []


# ---------- updated_at bumps ----------

def test_note_edit_bumps_updated_at(user_client):
    client, _ = user_client
    with session_scope(_factory()) as s:
        bob = s.scalar(select(User).where(User.username == "bob"))
        bob_id = bob.id
    tid = _create_topic(bob_id, "topic")
    page_id = _page_id("byte-1985-12", 1)

    old = datetime.utcnow() - timedelta(days=3)
    with session_scope(_factory()) as s:
        s.add(ResearchTopicPage(topic_id=tid, page_id=page_id, note="old", saved_at=old))
        s.get(ResearchTopic, tid).updated_at = old

    token = get_csrf(client)
    resp = client.post(
        f"/research/{tid}/pages/{page_id}/note",
        data={"csrf_token": token, "note": "new note"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope(_factory()) as s:
        t = s.get(ResearchTopic, tid)
        assert t.updated_at > old + timedelta(days=2)


def test_page_remove_bumps_updated_at(user_client):
    client, _ = user_client
    with session_scope(_factory()) as s:
        bob = s.scalar(select(User).where(User.username == "bob"))
        bob_id = bob.id
    tid = _create_topic(bob_id, "topic")
    page_id = _page_id("byte-1985-12", 1)

    old = datetime.utcnow() - timedelta(days=3)
    with session_scope(_factory()) as s:
        s.add(ResearchTopicPage(topic_id=tid, page_id=page_id, note=None, saved_at=old))
        s.get(ResearchTopic, tid).updated_at = old

    token = get_csrf(client)
    resp = client.post(
        f"/research/{tid}/pages/{page_id}/remove",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope(_factory()) as s:
        rows = s.scalars(
            select(ResearchTopicPage).where(
                ResearchTopicPage.topic_id == tid
            )
        ).all()
        assert rows == []
        t = s.get(ResearchTopic, tid)
        assert t.updated_at > old + timedelta(days=2)


# ---------- Cascade behaviour ----------

def test_deleting_magazine_cascades_topic_pages_but_keeps_topic(admin_client):
    client, _ = admin_client
    with session_scope(_factory()) as s:
        alice = s.scalar(select(User).where(User.username == "alice"))
        alice_id = alice.id
    tid = _create_topic(alice_id, "doomed mag's pages")

    page_id = _page_id("compute-1984-06", 1)
    now = datetime.utcnow()
    with session_scope(_factory()) as s:
        s.add(ResearchTopicPage(topic_id=tid, page_id=page_id, note="hi", saved_at=now))

    token = get_csrf(client)
    resp = client.post(
        "/admin/issues/compute-1984-06/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with session_scope(_factory()) as s:
        # Topic stays.
        assert s.get(ResearchTopic, tid) is not None
        # Saved page row is gone (cascade via pages → research_topic_pages).
        assert s.scalar(
            select(ResearchTopicPage).where(ResearchTopicPage.topic_id == tid)
        ) is None


def test_deleting_user_cascades_topics_and_saves(admin_client):
    client, _ = admin_client
    bob_id = create_user(_factory(), "bob", "bob-pw")
    tid = _create_topic(bob_id, "bob's stuff")
    page_id = _page_id("byte-1985-12", 1)
    now = datetime.utcnow()
    with session_scope(_factory()) as s:
        s.add(ResearchTopicPage(topic_id=tid, page_id=page_id, note=None, saved_at=now))

    token = get_csrf(client)
    resp = client.post(
        f"/admin/users/{bob_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with session_scope(_factory()) as s:
        assert s.get(ResearchTopic, tid) is None
        assert s.scalar(
            select(ResearchTopicPage).where(ResearchTopicPage.topic_id == tid)
        ) is None


def test_duplicate_topic_page_constraint(user_client):
    """Same (topic_id, page_id) twice must raise IntegrityError."""
    client, _ = user_client
    with session_scope(_factory()) as s:
        bob = s.scalar(select(User).where(User.username == "bob"))
        bob_id = bob.id
    tid = _create_topic(bob_id, "topic")
    page_id = _page_id("byte-1985-12", 1)
    now = datetime.utcnow()
    with session_scope(_factory()) as s:
        s.add(ResearchTopicPage(topic_id=tid, page_id=page_id, note=None, saved_at=now))

    with pytest.raises(IntegrityError):
        with session_scope(_factory()) as s:
            s.add(ResearchTopicPage(
                topic_id=tid, page_id=page_id, note=None, saved_at=now
            ))


# ---------- Detail view rendering ----------

def test_topic_detail_renders_grouped_pages_with_notes(user_client):
    client, _ = user_client
    with session_scope(_factory()) as s:
        bob = s.scalar(select(User).where(User.username == "bob"))
        bob_id = bob.id
    tid = _create_topic(bob_id, "mixed")

    # Save pages from both magazines so we can verify grouping.
    p_byte_1 = _page_id("byte-1985-12", 1)
    p_byte_2 = _page_id("byte-1985-12", 2)
    p_compute = _page_id("compute-1984-06", 1)

    now = datetime.utcnow()
    with session_scope(_factory()) as s:
        s.add(ResearchTopicPage(
            topic_id=tid, page_id=p_byte_1, note="byte page 1 note", saved_at=now
        ))
        s.add(ResearchTopicPage(
            topic_id=tid, page_id=p_byte_2, note=None, saved_at=now
        ))
        s.add(ResearchTopicPage(
            topic_id=tid, page_id=p_compute, note="compute page", saved_at=now
        ))

    resp = client.get(f"/research/{tid}")
    assert resp.status_code == 200
    # Magazine headings both rendered.
    assert "Byte" in resp.text
    assert "Compute" in resp.text
    # Notes rendered.
    assert "byte page 1 note" in resp.text
    assert "compute page" in resp.text
    # Page numbers + links back to the page view.
    assert "/magazine/byte-1985-12/page/1" in resp.text
    assert "/magazine/byte-1985-12/page/2" in resp.text
    assert "/magazine/compute-1984-06/page/1" in resp.text


def test_page_viewer_pre_checks_existing_topics(user_client):
    client, _ = user_client
    with session_scope(_factory()) as s:
        bob = s.scalar(select(User).where(User.username == "bob"))
        bob_id = bob.id
    tid = _create_topic(bob_id, "already-here")
    page_id = _page_id("byte-1985-12", 1)
    now = datetime.utcnow()
    with session_scope(_factory()) as s:
        s.add(ResearchTopicPage(
            topic_id=tid, page_id=page_id, note=None, saved_at=now
        ))

    resp = client.get("/magazine/byte-1985-12/page/1")
    assert resp.status_code == 200
    # The checkbox for this topic should be checked.
    # Look for the input with value=tid AND checked.
    import re
    m = re.search(
        r'<input[^>]*name="topic_ids"[^>]*value="' + str(tid) + r'"[^>]*>',
        resp.text,
    )
    assert m is not None, "topic checkbox missing"
    assert "checked" in m.group(0), "existing topic should be pre-checked"
