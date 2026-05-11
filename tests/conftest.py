from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.importer import import_bundle
from magsearch.ingest.ocr import FakeOCREngine, OCRRegion
from magsearch.ingest.pipeline import IngestOptions, IngestPipeline
from magsearch.web.app import create_app
from tests.fixtures.pdfs import make_pdf


def _ingest_two_magazines(bundles_dir: Path, src_dir: Path):
    """Create two distinct test magazines with predictable text."""
    return [
        IngestPipeline(
            bundles_dir,
            FakeOCREngine(responses=[
                [OCRRegion(text="vintage synthesizer review", bbox=(0,0,50,10), confidence=1.0)],
                [OCRRegion(text="modem speeds compared", bbox=(0,0,50,10), confidence=1.0)],
            ]),
            IngestOptions(title="Byte", publisher="McGraw-Hill",
                          publication_date=date(1985, 12, 1)),
        ).run(make_pdf(src_dir / "byte.pdf", num_pages=2)),
        IngestPipeline(
            bundles_dir,
            FakeOCREngine(responses=[
                [OCRRegion(text="apple ii basic", bbox=(0,0,50,10), confidence=1.0)],
            ]),
            IngestOptions(title="Compute", publisher="ABC Publishing",
                          publication_date=date(1984, 6, 1)),
        ).run(make_pdf(src_dir / "compute.pdf", num_pages=1)),
    ]


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    bundles = tmp_path / "bundles"
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(bundles))
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")

    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)

    results = _ingest_two_magazines(bundles, src)
    with session_scope(factory) as s:
        for r in results:
            import_bundle(r.bundle_dir, s)

    # Clear deps cache so the test DB is used.
    from magsearch.web import deps
    deps._session_factory_for.cache_clear()

    return TestClient(create_app()), bundles
