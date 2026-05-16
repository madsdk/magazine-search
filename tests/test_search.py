from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.importer import import_bundle
from magsearch.ingest.ocr import FakeOCREngine, OCRRegion
from magsearch.ingest.pipeline import IngestOptions, IngestPipeline
from magsearch.web.search import (
    sanitize_query,
    search,
    search_in_magazine,
    search_in_magazine_title,
    search_magazines,
)
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


@pytest.fixture
def two_magazines_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    bundles = tmp_path / "bundles"

    # "Byte" has two matching pages for "synthesizer".
    byte = IngestPipeline(
        bundles, FakeOCREngine(responses=[
            [OCRRegion(text="vintage synthesizer review", bbox=(0,0,50,10), confidence=1.0)],
            [OCRRegion(text="modem speeds compared", bbox=(0,0,50,10), confidence=1.0)],
            [OCRRegion(text="synthesizer keyboards in 1985", bbox=(0,0,50,10), confidence=1.0)],
        ]),
        IngestOptions(title="Byte", publication_date=date(1985, 12, 1)),
    ).run(make_pdf(tmp_path / "byte.pdf", num_pages=3))

    # "Compute" has one matching page for "synthesizer".
    compute = IngestPipeline(
        bundles, FakeOCREngine(responses=[
            [OCRRegion(text="apple ii basic", bbox=(0,0,50,10), confidence=1.0)],
            [OCRRegion(text="synthesizer programming tutorial", bbox=(0,0,50,10), confidence=1.0)],
        ]),
        IngestOptions(title="Compute", publication_date=date(1984, 6, 1)),
    ).run(make_pdf(tmp_path / "compute.pdf", num_pages=2))

    with session_scope(factory) as s:
        import_bundle(byte.bundle_dir, s)
        import_bundle(compute.bundle_dir, s)
    return factory


def test_search_magazines_groups_by_magazine(two_magazines_db):
    factory = two_magazines_db
    with session_scope(factory) as s:
        hits = search_magazines(s, "synthesizer", offset=0, limit=10)
    assert len(hits) == 2
    by_id = {h.magazine_id: h for h in hits}
    assert by_id["byte-1985-12"].match_count == 2
    assert by_id["compute-1984-06"].match_count == 1
    # Cover path should be populated for the grouped row.
    assert all(h.cover_path for h in hits)


def test_search_magazines_empty_query_returns_empty(two_magazines_db):
    factory = two_magazines_db
    with session_scope(factory) as s:
        assert search_magazines(s, "", offset=0, limit=10) == []
        assert search_magazines(s, "!@#$", offset=0, limit=10) == []


def test_search_in_magazine_filters_to_one_issue(two_magazines_db):
    factory = two_magazines_db
    with session_scope(factory) as s:
        hits = search_in_magazine(s, "synthesizer", "byte-1985-12",
                                  offset=0, limit=10)
    assert len(hits) == 2
    assert all(h.magazine_id == "byte-1985-12" for h in hits)
    assert {h.page_number for h in hits} == {1, 3}
    assert all("<mark>" in h.snippet for h in hits)


def test_search_in_magazine_empty_for_unknown_id(two_magazines_db):
    factory = two_magazines_db
    with session_scope(factory) as s:
        assert search_in_magazine(s, "synthesizer", "nonexistent",
                                  offset=0, limit=10) == []


def test_search_in_magazine_empty_query_returns_empty(two_magazines_db):
    factory = two_magazines_db
    with session_scope(factory) as s:
        assert search_in_magazine(s, "", "byte-1985-12",
                                  offset=0, limit=10) == []


def test_search_sort_newest_orders_by_date_desc(two_magazines_db):
    # Byte = 1985-12, Compute = 1984-06. sort=newest should put Byte first.
    factory = two_magazines_db
    with session_scope(factory) as s:
        hits = search(s, "synthesizer", offset=0, limit=10, sort="newest")
    assert len(hits) == 3
    assert hits[0].magazine_id == "byte-1985-12"
    assert hits[-1].magazine_id == "compute-1984-06"


def test_search_sort_oldest_orders_by_date_asc(two_magazines_db):
    factory = two_magazines_db
    with session_scope(factory) as s:
        hits = search(s, "synthesizer", offset=0, limit=10, sort="oldest")
    assert len(hits) == 3
    assert hits[0].magazine_id == "compute-1984-06"
    assert hits[-1].magazine_id == "byte-1985-12"


def test_search_invalid_sort_falls_back_to_rank(two_magazines_db):
    factory = two_magazines_db
    with session_scope(factory) as s:
        bogus = search(s, "synthesizer", offset=0, limit=10, sort="bogus")
        default = search(s, "synthesizer", offset=0, limit=10)
    assert [(h.magazine_id, h.page_number) for h in bogus] == \
           [(h.magazine_id, h.page_number) for h in default]


def test_search_in_magazine_sort_page_orders_ascending(populated_db):
    # populated_db Byte issue has matches on pages 1 and 3.
    factory = populated_db
    with session_scope(factory) as s:
        hits = search_in_magazine(
            s, "synthesizer", "byte-1985-12",
            offset=0, limit=10, sort="page",
        )
    assert [h.page_number for h in hits] == [1, 3]


def test_search_in_magazine_title_sort_newest(two_magazines_db):
    # Per-title with sort=newest. Use title "Byte" — only one issue so
    # mostly verifies the SQL doesn't blow up and returns expected pages.
    factory = two_magazines_db
    with session_scope(factory) as s:
        hits = search_in_magazine_title(
            s, "synthesizer", "Byte", offset=0, limit=10, sort="newest",
        )
    assert {h.page_number for h in hits} == {1, 3}


def test_search_magazines_sort_newest(two_magazines_db):
    # Grouped view: Byte (1985-12) should come before Compute (1984-06)
    # when sorted newest-first.
    factory = two_magazines_db
    with session_scope(factory) as s:
        groups = search_magazines(
            s, "synthesizer", offset=0, limit=10, sort="newest",
        )
    assert len(groups) == 2
    assert groups[0].magazine_id == "byte-1985-12"
    assert groups[1].magazine_id == "compute-1984-06"
