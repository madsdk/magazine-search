from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text

from alembic import command
from magsearch.bundle_edit import BundleEditError, apply_drop, plan_drop, resync_magazine
from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.importer import import_bundle
from magsearch.manifest import Manifest, PageEntry
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


def test_resync_shift_survives_id_page_number_inversion(factory):
    """Pins the two-pass negation shift against a regression: a naive
    single-pass `page_number -= count` happens to work whenever Page.id
    ascends in the same order as page_number (the case for every other test
    in this file, since import_bundle assigns ids in page_number order) —
    SQLAlchemy's unit of work flushes dirty UPDATEs in ascending primary-key
    order, so ascending ids there means the lowest page_number is always
    vacated before a higher one needs its slot.

    Build a magazine directly with Page rows inserted in descending
    page_number order, so id 1 holds the *highest* surviving page_number and
    the flush therefore processes it first — forcing a naive shift to move a
    high number into a slot a not-yet-updated lower survivor still occupies.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(factory) as s:
        s.add(Magazine(
            id="inv-1985-01",
            title="Inv",
            issue=None,
            publication_date=date(1985, 1, 1),
            publisher=None,
            original_filename="inv.pdf",
            original_format="pdf",
            page_count=4,
            content_hash="deadbeef",
            cover_path="inv-1985-01/cover.webp",
            ocr_engine="fake",
            ocr_engine_version="1",
            ingested_at=now,
        ))
        s.flush()
        # Descending insert order: id 1 -> page_number 4, ..., id 4 -> page_number 1.
        for page_number in (4, 3, 2, 1):
            s.add(Page(
                magazine_id="inv-1985-01",
                page_number=page_number,
                image_path=f"inv-1985-01/pages/{page_number:04d}.webp",
                thumb_path=f"inv-1985-01/thumbs/{page_number:04d}.webp",
                text=f"word-{page_number}",
            ))

    with session_scope(factory) as s:
        ids_by_number = {
            p.page_number: p.id
            for p in s.scalars(select(Page).where(Page.magazine_id == "inv-1985-01")).all()
        }
    # Confirm the fixture actually inverts id order relative to page_number
    # order before trusting it to exercise anything.
    assert [ids_by_number[n] for n in (1, 2, 3, 4)] == [4, 3, 2, 1]

    # A matching manifest: page 1 was dropped, survivors 2/3/4 renumbered 1/2/3.
    manifest = Manifest(
        schema_version=1,
        id="inv-1985-01",
        title="Inv",
        issue=None,
        publication_date=date(1985, 1, 1),
        publisher=None,
        original_filename="inv.pdf",
        original_format="pdf",
        page_count=3,
        content_hash="deadbeef",
        ocr_engine="fake",
        ocr_engine_version="1",
        cover_path="cover.webp",
        pages=[
            PageEntry(page_number=1, image_path="pages/0001.webp",
                      thumb_path="thumbs/0001.webp", ocr_path="ocr/0001.txt", text="word-2"),
            PageEntry(page_number=2, image_path="pages/0002.webp",
                      thumb_path="thumbs/0002.webp", ocr_path="ocr/0002.txt", text="word-3"),
            PageEntry(page_number=3, image_path="pages/0003.webp",
                      thumb_path="thumbs/0003.webp", ocr_path="ocr/0003.txt", text="word-4"),
        ],
        checksums=[],
    )

    with session_scope(factory) as s:
        assert resync_magazine(s, manifest, count=1) is True

    with session_scope(factory) as s:
        pages = s.scalars(
            select(Page).where(Page.magazine_id == "inv-1985-01").order_by(Page.page_number)
        ).all()
        assert [p.page_number for p in pages] == [1, 2, 3]
        assert [p.image_path for p in pages] == [
            "inv-1985-01/pages/0001.webp",
            "inv-1985-01/pages/0002.webp",
            "inv-1985-01/pages/0003.webp",
        ]


def test_resync_handles_count_two(tmp_path, factory):
    bundle = _imported(tmp_path, factory, num_pages=4)
    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=2))

    with session_scope(factory) as s:
        resync_magazine(s, manifest, count=2)

    with session_scope(factory) as s:
        pages = s.scalars(select(Page).order_by(Page.page_number)).all()
        assert [p.page_number for p in pages] == [1, 2]
        assert s.get(Magazine, manifest.id).page_count == 2


def test_resync_reports_drift_between_rows_and_manifest(tmp_path, factory):
    """Unguarded, the manifest lookup raises a bare KeyError, which the CLI
    renders as `! byte-1985-12: 2` — the key as the entire message, in the one
    situation where the operator needs to be told what drifted."""
    bundle = _imported(tmp_path, factory)
    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))
    # Two surviving rows, a manifest that only knows about one of them.
    short = manifest.model_copy(update={"page_count": 1, "pages": manifest.pages[:1]})

    with pytest.raises(BundleEditError) as exc:
        with session_scope(factory) as s:
            resync_magazine(s, short, count=1)

    message = str(exc.value)
    assert "2 page row(s)" in message
    assert "1 page(s)" in message
    assert f"magsearch check {manifest.id}" in message
