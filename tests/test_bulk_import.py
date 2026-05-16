from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from magsearch.bulk_import import (
    DONE,
    FAILED,
    ImportStateLog,
    bulk_import,
    discover_bundles,
)
from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.ingest.ocr import FakeOCREngine, OCRRegion
from magsearch.ingest.pipeline import IngestOptions, IngestPipeline
from magsearch.models import Magazine
from tests.fixtures.pdfs import make_pdf


@pytest.fixture
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    return make_session_factory(make_engine(f"sqlite:///{db_path}"))


def _ingest(bundles: Path, src: Path, title: str, dt: date, pages: int = 1):
    engine = FakeOCREngine(responses=[
        [OCRRegion(text=f"page {i+1}", bbox=(0, 0, 50, 10), confidence=1.0)]
        for i in range(pages)
    ])
    return IngestPipeline(
        bundles_root=bundles,
        ocr_engine=engine,
        options=IngestOptions(title=title, publication_date=dt),
    ).run(make_pdf(src, num_pages=pages))


def test_discover_bundles_skips_dirs_without_manifest(tmp_path):
    (tmp_path / "valid").mkdir()
    (tmp_path / "valid" / "manifest.json").write_text("{}")
    (tmp_path / "empty").mkdir()
    found = discover_bundles(tmp_path)
    assert [p.name for p in found] == ["valid"]


def test_bulk_import_happy_path(tmp_path, db):
    factory = db
    bundles = tmp_path / "bundles"
    src = tmp_path / "src"
    src.mkdir()
    _ingest(bundles, src / "alpha.pdf", "Alpha", date(1985, 1, 1))
    _ingest(bundles, src / "beta.pdf", "Beta", date(1985, 2, 1))

    result = bulk_import(
        bundles_dir=bundles, state_path=None,
        session_factory=factory,
    )
    assert result.succeeded == 2
    with session_scope(factory) as s:
        ids = {m.id for m in s.scalars(select(Magazine)).all()}
    assert ids == {"alpha-1985-01", "beta-1985-02"}


def test_bulk_import_idempotent(tmp_path, db):
    factory = db
    bundles = tmp_path / "bundles"
    src = tmp_path / "src"
    src.mkdir()
    _ingest(bundles, src / "alpha.pdf", "Alpha", date(1985, 1, 1))

    bulk_import(bundles_dir=bundles, state_path=None, session_factory=factory)
    # Wipe the state log so the second run actually re-attempts import (it
    # would otherwise see status=done and skip without engaging import_bundle).
    (bundles / ".bulk-import-state.jsonl").unlink()
    bulk_import(bundles_dir=bundles, state_path=None, session_factory=factory)

    with session_scope(factory) as s:
        n = s.scalar(select(func.count()).select_from(Magazine))
    assert n == 1


def test_bulk_import_resumes_after_partial(tmp_path, db):
    factory = db
    bundles = tmp_path / "bundles"
    src = tmp_path / "src"
    src.mkdir()
    r1 = _ingest(bundles, src / "alpha.pdf", "Alpha", date(1985, 1, 1))
    r2 = _ingest(bundles, src / "beta.pdf", "Beta", date(1985, 2, 1))

    # Pre-seed state: beta is already done; bulk_import shouldn't re-import it.
    state_path = bundles / ".bulk-import-state.jsonl"
    log = ImportStateLog(state_path)
    e = log.get(r2.bundle_dir.name)
    e.status = DONE
    e.magazine_id = "beta-1985-02"
    log.save()

    result = bulk_import(
        bundles_dir=bundles, state_path=None, session_factory=factory,
    )
    assert result.succeeded == 1  # alpha
    assert result.skipped == 1    # beta
    # Only alpha ended up in DB (beta was marked done in state, never imported here).
    with session_scope(factory) as s:
        ids = {m.id for m in s.scalars(select(Magazine)).all()}
    assert ids == {"alpha-1985-01"}


def test_bulk_import_retry_failed(tmp_path, db):
    factory = db
    bundles = tmp_path / "bundles"
    src = tmp_path / "src"
    src.mkdir()
    r = _ingest(bundles, src / "alpha.pdf", "Alpha", date(1985, 1, 1))

    log = ImportStateLog(bundles / ".bulk-import-state.jsonl")
    e = log.get(r.bundle_dir.name)
    e.status = FAILED
    e.error = "synthetic prior failure"
    log.save()

    # Without --retry-failed it skips.
    r1 = bulk_import(bundles_dir=bundles, state_path=None, session_factory=factory)
    assert r1.skipped == 1 and r1.succeeded == 0

    # With --retry-failed it runs.
    r2 = bulk_import(
        bundles_dir=bundles, state_path=None, session_factory=factory,
        retry_failed=True,
    )
    assert r2.succeeded == 1


