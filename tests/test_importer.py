from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.importer import ImportError as MagImportError, import_bundle, resolve_magazines, ResolvedTargets
from magsearch.ingest.ocr import FakeOCREngine, OCRRegion
from magsearch.ingest.pipeline import IngestOptions, IngestPipeline
from magsearch.models import Magazine, Page
from tests.fixtures.pdfs import make_pdf


def _ingested_bundle(tmp_path: Path, title: str = "Byte", num_pages: int = 2) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    src = make_pdf(tmp_path / f"{title}.pdf", num_pages=num_pages)
    bundles = tmp_path / "bundles"
    pipeline = IngestPipeline(
        bundles_root=bundles,
        ocr_engine=FakeOCREngine(responses=[
            [OCRRegion(text=f"unique-word-{i + 1}", bbox=(0, 0, 50, 10), confidence=1.0)]
            for i in range(num_pages)
        ]),
        options=IngestOptions(title=title, publication_date=date(1985, 12, 1)),
    )
    result = pipeline.run(src)
    return result.bundle_dir


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = make_engine(f"sqlite:///{db_path}")
    return make_session_factory(engine), engine


def test_import_inserts_magazine_and_pages(tmp_path, db):
    factory, engine = db
    bundle = _ingested_bundle(tmp_path, num_pages=2)
    with session_scope(factory) as s:
        import_bundle(bundle, s)
    with session_scope(factory) as s:
        m = s.scalar(select(Magazine).where(Magazine.id == "byte-1985-12"))
        assert m is not None
        assert m.page_count == 2
        pages = s.scalars(select(Page).where(Page.magazine_id == m.id).order_by(Page.page_number)).all()
        assert [p.page_number for p in pages] == [1, 2]
        assert pages[0].text == "unique-word-1"


def test_import_is_idempotent(tmp_path, db):
    factory, engine = db
    bundle = _ingested_bundle(tmp_path)
    with session_scope(factory) as s:
        import_bundle(bundle, s)
    with session_scope(factory) as s:
        import_bundle(bundle, s)  # second time
    with engine.connect() as conn:
        n_mags = conn.execute(text("SELECT COUNT(*) FROM magazines")).scalar_one()
        n_pages = conn.execute(text("SELECT COUNT(*) FROM pages")).scalar_one()
    assert n_mags == 1
    assert n_pages == 2


def test_reimport_preserves_ingested_at(tmp_path, db):
    factory, _ = db
    bundle = _ingested_bundle(tmp_path)
    with session_scope(factory) as s:
        import_bundle(bundle, s)
    with session_scope(factory) as s:
        first = s.scalar(select(Magazine).where(Magazine.id == "byte-1985-12"))
        original_ingested_at = first.ingested_at
    with session_scope(factory) as s:
        import_bundle(bundle, s)
    with session_scope(factory) as s:
        again = s.scalar(select(Magazine).where(Magazine.id == "byte-1985-12"))
        assert again.ingested_at == original_ingested_at


def test_import_detects_slug_collision(tmp_path, db):
    factory, _ = db
    b1 = _ingested_bundle(tmp_path / "first", title="Byte", num_pages=1)
    b2_src_dir = tmp_path / "second"
    b2_src_dir.mkdir()
    bundle2 = _ingested_bundle(b2_src_dir, title="Byte", num_pages=2)
    # Force both bundles to claim the same id by editing manifest of #2:
    import json
    m1_id = json.loads((b1 / "manifest.json").read_text())["id"]
    m2 = json.loads((bundle2 / "manifest.json").read_text())
    m2["id"] = m1_id
    (bundle2 / "manifest.json").write_text(json.dumps(m2))

    with session_scope(factory) as s:
        import_bundle(b1, s)
    with pytest.raises(MagImportError, match="collision"):
        with session_scope(factory) as s:
            import_bundle(bundle2, s)


def test_import_rejects_corrupt_bundle(tmp_path, db):
    factory, _ = db
    bundle = _ingested_bundle(tmp_path)
    # Corrupt one of the page images.
    page_file = bundle / "pages" / "0001.webp"
    page_file.write_bytes(b"corrupted")
    with pytest.raises(MagImportError, match="checksum"):
        with session_scope(factory) as s:
            import_bundle(bundle, s)


def _import(factory, bundle):
    with session_scope(factory) as s:
        import_bundle(bundle, s)


def test_resolve_by_id_found_and_missing(tmp_path, db):
    factory, _ = db
    _import(factory, _ingested_bundle(tmp_path, title="Byte", num_pages=1))
    with session_scope(factory) as s:
        r = resolve_magazines(s, ["byte-1985-12", "nope-1999-01"], None)
        assert [m.id for m in r.found] == ["byte-1985-12"]
        assert r.not_found == ["nope-1999-01"]


def test_resolve_by_title_case_insensitive_multiple(tmp_path, db):
    factory, _ = db
    _import(factory, _ingested_bundle(tmp_path / "a", title="Byte", num_pages=1))
    # Second issue, same title, different date -> different id.
    (tmp_path / "b").mkdir(parents=True, exist_ok=True)
    src = make_pdf(tmp_path / "b" / "Byte.pdf", num_pages=1)
    from magsearch.ingest.ocr import FakeOCREngine, OCRRegion
    from magsearch.ingest.pipeline import IngestOptions, IngestPipeline
    pipeline = IngestPipeline(
        bundles_root=tmp_path / "b" / "bundles",
        ocr_engine=FakeOCREngine(responses=[[OCRRegion(text="w", bbox=(0, 0, 5, 5), confidence=1.0)]]),
        options=IngestOptions(title="Byte", publication_date=date(1986, 1, 1)),
    )
    _import(factory, pipeline.run(src).bundle_dir)
    with session_scope(factory) as s:
        r = resolve_magazines(s, [], "byte")
        assert {m.id for m in r.found} == {"byte-1985-12", "byte-1986-01"}
        assert r.not_found == []


def test_resolve_dedupes_id_and_title(tmp_path, db):
    factory, _ = db
    _import(factory, _ingested_bundle(tmp_path, title="Byte", num_pages=1))
    with session_scope(factory) as s:
        r = resolve_magazines(s, ["byte-1985-12"], "Byte")
        assert [m.id for m in r.found] == ["byte-1985-12"]  # not duplicated
