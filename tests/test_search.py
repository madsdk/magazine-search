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


def test_sanitize_query_match_any_uses_or():
    # When match_all=False, top-level parts are OR'd (FTS5 OR is uppercase).
    assert sanitize_query("hello world", match_all=False) == "hello OR world"
    assert sanitize_query("one two three", match_all=False) == "one OR two OR three"
    # A single term has nothing to OR with.
    assert sanitize_query("hello", match_all=False) == "hello"
    # Quoted phrases remain phrases, OR'd against bare words.
    assert sanitize_query('"vintage synth" modem', match_all=False) == \
        '"vintage synth" OR modem'


def test_sanitize_query_match_phrase_wraps_everything():
    # match_phrase=True collapses every word into one sequential phrase,
    # regardless of existing quotes.
    assert sanitize_query("this must be like that", match_phrase=True) == \
        '"this must be like that"'
    assert sanitize_query('"vintage synth" modem', match_phrase=True) == \
        '"vintage synth modem"'
    # match_phrase overrides match_all (phrase implies AND in order).
    assert sanitize_query("a b c", match_all=False, match_phrase=True) == \
        '"a b c"'
    # Empty / junk inputs stay empty.
    assert sanitize_query("", match_phrase=True) == ""
    assert sanitize_query("!@#", match_phrase=True) == ""


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


def test_search_magazines_sort_matches_orders_by_count(two_magazines_db):
    # Byte has 2 matching pages, Compute has 1. With sort="matches", Byte
    # must come first regardless of publication date.
    factory = two_magazines_db
    with session_scope(factory) as s:
        groups = search_magazines(
            s, "synthesizer", offset=0, limit=10, sort="matches",
        )
    assert [g.magazine_id for g in groups] == ["byte-1985-12", "compute-1984-06"]
    assert [g.match_count for g in groups] == [2, 1]

    # Sanity check that the existing oldest sort still puts Compute first —
    # this confirms the new sort is ordering by count, not by date.
    with session_scope(factory) as s:
        oldest = search_magazines(
            s, "synthesizer", offset=0, limit=10, sort="oldest",
        )
    assert oldest[0].magazine_id == "compute-1984-06"


def test_search_match_all_requires_every_term(populated_db):
    # Byte fixture: p1="vintage synthesizer review", p3="synthesizer keyboards in 1985".
    # Only p3 contains both "synthesizer" and "keyboards"; p1 only has "synthesizer".
    factory = populated_db
    with session_scope(factory) as s:
        hits = search(s, "synthesizer keyboards", offset=0, limit=10)
    assert {h.page_number for h in hits} == {3}


def test_search_match_any_term_returns_either(populated_db):
    # Same fixture; "modem" appears on p2, "synthesizer" on p1+p3.
    # match_all=False should return all three pages.
    factory = populated_db
    with session_scope(factory) as s:
        hits = search(s, "modem synthesizer", offset=0, limit=10, match_all=False)
    assert {h.page_number for h in hits} == {1, 2, 3}


def test_search_match_phrase_requires_order(populated_db):
    # p1 = "vintage synthesizer review" — words in order should match.
    # The reversed phrase should not.
    factory = populated_db
    with session_scope(factory) as s:
        in_order = search(s, "vintage synthesizer", offset=0, limit=10,
                          match_phrase=True)
        reversed_ = search(s, "synthesizer vintage", offset=0, limit=10,
                           match_phrase=True)
        # Without match_phrase, both terms are present on p1 so it matches.
        unordered = search(s, "synthesizer vintage", offset=0, limit=10)
    assert {h.page_number for h in in_order} == {1}
    assert reversed_ == []
    assert {h.page_number for h in unordered} == {1}


def test_search_match_phrase_overrides_match_all_off(populated_db):
    # Even with match_all=False, match_phrase=True collapses the query
    # into a single phrase — so a non-matching order returns nothing.
    factory = populated_db
    with session_scope(factory) as s:
        hits = search(s, "keyboards synthesizer", offset=0, limit=10,
                      match_all=False, match_phrase=True)
    assert hits == []


@pytest.fixture
def tied_match_count_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    bundles = tmp_path / "bundles"

    # "Crisp" — one matching page, term in isolation → better (smaller) bm25 rank.
    crisp = IngestPipeline(
        bundles, FakeOCREngine(responses=[
            [OCRRegion(text="synthesizer", bbox=(0,0,50,10), confidence=1.0)],
        ]),
        IngestOptions(title="Crisp", publication_date=date(1985, 1, 1)),
    ).run(make_pdf(tmp_path / "crisp.pdf", num_pages=1))

    # "Noisy" — one matching page, term buried in many unrelated words → worse rank.
    noisy_text = (
        "apple commodore atari amiga modem floppy disk drive printer "
        "keyboard mouse joystick monitor cable cartridge cassette tape "
        "synthesizer review continues with lengthy unrelated commentary "
        "about hardware peripherals and software titles of the era"
    )
    noisy = IngestPipeline(
        bundles, FakeOCREngine(responses=[
            [OCRRegion(text=noisy_text, bbox=(0,0,500,10), confidence=1.0)],
        ]),
        IngestOptions(title="Noisy", publication_date=date(1986, 1, 1)),
    ).run(make_pdf(tmp_path / "noisy.pdf", num_pages=1))

    with session_scope(factory) as s:
        import_bundle(crisp.bundle_dir, s)
        import_bundle(noisy.bundle_dir, s)
    return factory


def test_search_magazines_sort_matches_tiebreak_by_rank(tied_match_count_db):
    # Both issues have match_count == 1. With sort="matches", the better-ranked
    # issue (Crisp — search term in isolation) must come first.
    factory = tied_match_count_db
    with session_scope(factory) as s:
        groups = search_magazines(
            s, "synthesizer", offset=0, limit=10, sort="matches",
        )
    assert len(groups) == 2
    assert all(g.match_count == 1 for g in groups)
    assert groups[0].magazine_id == "crisp-1985-01"
    assert groups[1].magazine_id == "noisy-1986-01"


@pytest.fixture
def identical_content_db(tmp_path, monkeypatch):
    """Two magazines with byte-identical OCR text — they produce the same
    match_count AND the same best_rank, so the only way to get a stable
    order is the magazines.id tie-breaker."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    engine = make_engine(f"sqlite:///{db_path}")
    factory = make_session_factory(engine)
    bundles = tmp_path / "bundles"

    def _ocr_responses():
        return [
            [OCRRegion(text="synthesizer review", bbox=(0,0,50,10), confidence=1.0)],
        ]

    # Insert "zebra" first so insertion order is the opposite of id-ascending
    # order — that way a test asserting id-ascending order can't pass simply
    # because SQLite happened to return rows in insertion order.
    zebra = IngestPipeline(
        bundles, FakeOCREngine(responses=_ocr_responses()),
        IngestOptions(title="Zebra", publication_date=date(1985, 1, 1)),
    ).run(make_pdf(tmp_path / "zebra.pdf", num_pages=1))

    alpha = IngestPipeline(
        bundles, FakeOCREngine(responses=_ocr_responses()),
        IngestOptions(title="Alpha", publication_date=date(1985, 1, 1)),
    ).run(make_pdf(tmp_path / "alpha.pdf", num_pages=1))

    with session_scope(factory) as s:
        import_bundle(zebra.bundle_dir, s)
        import_bundle(alpha.bundle_dir, s)
    return factory


def test_search_magazines_sort_matches_is_stable_on_total_tie(identical_content_db):
    # Both issues have identical match_count and best_rank. The magazines.id
    # tie-break must give a deterministic order — and "alpha-..." sorts before
    # "zebra-..." lexically, despite Zebra being inserted first.
    factory = identical_content_db
    with session_scope(factory) as s:
        groups = search_magazines(
            s, "synthesizer", offset=0, limit=10, sort="matches",
        )
    assert [g.magazine_id for g in groups] == ["alpha-1985-01", "zebra-1985-01"]
