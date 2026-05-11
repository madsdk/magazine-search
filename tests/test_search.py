from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.importer import import_bundle
from magsearch.ingest.ocr import FakeOCREngine, OCRRegion
from magsearch.ingest.pipeline import IngestOptions, IngestPipeline
from magsearch.web.search import sanitize_query, search
from tests.fixtures.pdfs import make_pdf


@pytest.fixture
def populated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    src = make_pdf(tmp_path / "byte.pdf", num_pages=3)
    bundles = tmp_path / "bundles"
    IngestPipeline(
        bundles, FakeOCREngine(responses=[
            [OCRRegion(text="vintage synthesizer review", bbox=(0,0,50,10), confidence=1.0)],
            [OCRRegion(text="modem speeds compared", bbox=(0,0,50,10), confidence=1.0)],
            [OCRRegion(text="synthesizer keyboards in 1985", bbox=(0,0,50,10), confidence=1.0)],
        ]),
        IngestOptions(title="Byte", publication_date=date(1985, 12, 1)),
    ).run(src)
    with session_scope(factory) as s:
        import_bundle(bundles / "byte-1985-12", s)
    return factory


def test_sanitize_query_strips_unsafe_chars():
    assert sanitize_query("hello world") == "hello world"
    assert sanitize_query("hello!@#world") == "hello world"
    assert sanitize_query('"quoted phrase"') == '"quoted phrase"'
    assert sanitize_query("") == ""
    assert sanitize_query("   ") == ""
    assert sanitize_query("!@#$%") == ""


def test_sanitize_query_balances_unmatched_quote():
    assert sanitize_query('"hello') == "hello"
    assert sanitize_query('hello"') == "hello"


def test_search_returns_stemmed_hits(populated_db):
    factory = populated_db
    with session_scope(factory) as s:
        results = search(s, "synthesizers", offset=0, limit=10)
    assert len(results) == 2
    assert {r.page_number for r in results} == {1, 3}
    assert all("<mark>" in r.snippet for r in results)
    assert all(r.magazine_title == "Byte" for r in results)


def test_search_phrase_query(populated_db):
    factory = populated_db
    with session_scope(factory) as s:
        results = search(s, '"vintage synthesizer"', offset=0, limit=10)
    assert len(results) == 1
    assert results[0].page_number == 1


def test_search_empty_query_returns_empty(populated_db):
    factory = populated_db
    with session_scope(factory) as s:
        results = search(s, "", offset=0, limit=10)
    assert results == []
