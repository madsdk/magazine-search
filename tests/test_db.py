from datetime import datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.models import Magazine, Page


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch) -> Config:
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_migration_creates_tables_and_fts(alembic_cfg, tmp_path):
    command.upgrade(alembic_cfg, "head")
    url = alembic_cfg.get_main_option("sqlalchemy.url")
    engine = make_engine(url)
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type IN ('table','trigger') ORDER BY name"
        )).all()
    names = {r[0] for r in rows}
    assert {"magazines", "pages", "pages_fts", "pages_ai", "pages_ad", "pages_au"} <= names


def test_fts_indexes_inserted_rows(alembic_cfg):
    command.upgrade(alembic_cfg, "head")
    url = alembic_cfg.get_main_option("sqlalchemy.url")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    with session_scope(factory) as s:
        m = Magazine(
            id="byte-1985-12", title="Byte", issue="Vol 10 No 12",
            publication_date=None, publisher="McGraw-Hill",
            original_filename="byte.pdf", original_format="pdf",
            page_count=1, content_hash="deadbeef",
            cover_path="byte-1985-12/cover.webp",
            ocr_engine="fake", ocr_engine_version="0.0.1",
            ingested_at=datetime(2026, 5, 11),
        )
        s.add(m)
        s.flush()
        s.add(Page(
            magazine_id="byte-1985-12", page_number=1,
            image_path="byte-1985-12/pages/0001.webp",
            thumb_path="byte-1985-12/thumbs/0001.webp",
            text="vintage synthesizer review",
        ))
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT rowid FROM pages_fts WHERE pages_fts MATCH 'synthesizer'"
        )).all()
        assert len(rows) == 1
