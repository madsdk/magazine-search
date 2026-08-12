from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text

from alembic import command
from magsearch.bundle_edit import apply_drop, plan_drop, resync_magazine
from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.importer import import_bundle
from magsearch.models import Magazine, Page, ResearchTopic, ResearchTopicPage, User
from magsearch.web.auth import hash_password
from tests.fixtures.bundles import make_bundle


@pytest.fixture
def factory(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    return make_session_factory(make_engine(f"sqlite:///{db_path}"))


def _imported(tmp_path, factory, num_pages=3) -> Path:
    bundle = make_bundle(tmp_path, num_pages=num_pages)
    with session_scope(factory) as s:
        import_bundle(bundle, s)
    return bundle


def test_resync_shifts_page_numbers_and_paths(tmp_path, factory):
    bundle = _imported(tmp_path, factory)
    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    with session_scope(factory) as s:
        assert resync_magazine(s, manifest, count=1) is True

    with session_scope(factory) as s:
        pages = s.scalars(
            select(Page).where(Page.magazine_id == manifest.id).order_by(Page.page_number)
        ).all()
        assert [p.page_number for p in pages] == [1, 2]
        assert [p.image_path for p in pages] == [
            f"{manifest.id}/pages/0001.webp",
            f"{manifest.id}/pages/0002.webp",
        ]
        assert [p.thumb_path for p in pages] == [
            f"{manifest.id}/thumbs/0001.webp",
            f"{manifest.id}/thumbs/0002.webp",
        ]


def test_resync_preserves_surviving_page_ids(tmp_path, factory):
    bundle = _imported(tmp_path, factory)
    with session_scope(factory) as s:
        ids_before = {
            p.page_number: p.id
            for p in s.scalars(select(Page).order_by(Page.page_number)).all()
        }

    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))
    with session_scope(factory) as s:
        resync_magazine(s, manifest, count=1)

    with session_scope(factory) as s:
        ids_after = {
            p.page_number: p.id
            for p in s.scalars(select(Page).order_by(Page.page_number)).all()
        }
    assert ids_after == {1: ids_before[2], 2: ids_before[3]}


def test_resync_updates_magazine_row(tmp_path, factory):
    bundle = _imported(tmp_path, factory)
    with session_scope(factory) as s:
        ingested_before = s.get(Magazine, bundle.name).ingested_at

    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))
    with session_scope(factory) as s:
        resync_magazine(s, manifest, count=1)

    with session_scope(factory) as s:
        mag = s.get(Magazine, manifest.id)
        assert mag.page_count == 2
        assert mag.cover_path == f"{manifest.id}/cover.webp"
        assert mag.ingested_at == ingested_before  # "Recently filed" must not churn


def test_resync_keeps_research_saves_on_surviving_pages(tmp_path, factory):
    bundle = _imported(tmp_path, factory)
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(factory) as s:
        s.add(User(username="r", password_hash=hash_password("x"),
                   is_researcher=True, created_at=now, last_login_at=None))
        s.flush()
        user_id = s.scalar(select(User.id))
        topic = ResearchTopic(user_id=user_id, name="t", description=None,
                              created_at=now, updated_at=now)
        s.add(topic)
        s.flush()
        page_one = s.scalar(select(Page).where(Page.page_number == 1))
        page_three = s.scalar(select(Page).where(Page.page_number == 3))
        kept_page_id = page_three.id
        s.add(ResearchTopicPage(topic_id=topic.id, page_id=page_one.id,
                                note="junk", saved_at=now))
        s.add(ResearchTopicPage(topic_id=topic.id, page_id=page_three.id,
                                note="keep", saved_at=now))

    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))
    with session_scope(factory) as s:
        resync_magazine(s, manifest, count=1)

    with session_scope(factory) as s:
        saves = s.scalars(select(ResearchTopicPage)).all()
        assert [sv.note for sv in saves] == ["keep"]
        assert saves[0].page_id == kept_page_id
        assert s.get(Page, kept_page_id).page_number == 2


def test_resync_keeps_fts_consistent(tmp_path, factory):
    """make_bundle's FakeOCR writes 'word-N' on page N — unique per page."""
    bundle = _imported(tmp_path, factory)
    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    with session_scope(factory) as s:
        resync_magazine(s, manifest, count=1)

    with session_scope(factory) as s:
        # Quoted as a phrase: FTS5's query-syntax parser treats an unquoted
        # hyphen-digit tail (e.g. bare `word-3`) as column-filter syntax and
        # raises "no such column: 3" — unrelated to resync_magazine, and
        # reproducible against a bare in-memory FTS5 table. Quoting searches
        # for the same literal token without tripping that parser rule.
        hits = s.execute(text(
            "SELECT pages.page_number FROM pages_fts "
            "JOIN pages ON pages_fts.rowid = pages.id WHERE pages_fts MATCH '\"word-3\"'"
        )).all()
        assert [h[0] for h in hits] == [2]
        gone = s.execute(text(
            "SELECT COUNT(*) FROM pages_fts WHERE pages_fts MATCH '\"word-1\"'"
        )).scalar()
        assert gone == 0
        s.execute(text("INSERT INTO pages_fts(pages_fts) VALUES ('integrity-check')"))


def test_resync_reports_a_missing_magazine_row(tmp_path, factory):
    bundle = make_bundle(tmp_path, num_pages=3)  # never imported
    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    with session_scope(factory) as s:
        assert resync_magazine(s, manifest, count=1) is False


def test_resync_handles_count_two(tmp_path, factory):
    bundle = _imported(tmp_path, factory, num_pages=4)
    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=2))

    with session_scope(factory) as s:
        resync_magazine(s, manifest, count=2)

    with session_scope(factory) as s:
        pages = s.scalars(select(Page).order_by(Page.page_number)).all()
        assert [p.page_number for p in pages] == [1, 2]
        assert s.get(Magazine, manifest.id).page_count == 2
