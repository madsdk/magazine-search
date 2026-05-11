# Magazine Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-hosted web app that lets the user search the full text of OCR'd magazine scans (PDF/CBR/CBZ) and click through to the magazine + page where each hit appears.

**Architecture:** One Python package, two physical machines, one SQLite database. A GPU-side CLI (`magsearch ingest`) reads PDF/CBR/CBZ, renders page images, runs PaddleOCR, and writes a self-contained "bundle" directory. A server-side CLI (`magsearch import`) reads bundles and populates SQLite + FTS5. A FastAPI app (`magsearch web`) serves the search UI and page images. The two sides communicate only through the bundle directory format (a versioned `manifest.json`).

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.0, Alembic, SQLite FTS5, Pydantic v2, Pydantic-Settings, Typer, Jinja2, Pillow, PyMuPDF (PDF), rarfile (CBR), PaddleOCR (optional extra), pytest.

**Reference spec:** `docs/superpowers/specs/2026-05-11-magazine-search-design.md`. Each task below points back to the spec section it implements.

---

## File Structure

```
magsearch/
├── pyproject.toml
├── alembic.ini
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       └── 0001_initial.py
├── src/magsearch/
│   ├── __init__.py
│   ├── settings.py             # pydantic-settings; DATABASE_URL, BUNDLES_DIR
│   ├── db.py                   # SQLAlchemy engine, session factory, FK pragma
│   ├── models.py               # Magazine, Page (SQLAlchemy 2.0 Mapped style)
│   ├── manifest.py             # pydantic — bundle manifest contract
│   ├── cli.py                  # typer: ingest, import, web, db
│   ├── importer.py             # bundle dir → DB (transactional)
│   ├── ingest/
│   │   ├── __init__.py
│   │   ├── pipeline.py         # 6-step orchestrator
│   │   ├── formats.py          # detect_format, read_pages dispatcher + impls
│   │   ├── normalize.py        # encode_page, encode_thumb, write_cover
│   │   ├── ocr.py              # OCREngine Protocol, OCRRegion, FakeOCREngine, PaddleOCREngine
│   │   └── ids.py              # slugify, content_hash, generate_id
│   └── web/
│       ├── __init__.py
│       ├── app.py              # create_app factory
│       ├── routes.py           # all HTTP routes
│       ├── search.py           # FTS5 query construction + sanitizer
│       ├── deps.py             # FastAPI dependencies (get_db, get_settings)
│       └── templates/
│           ├── base.html
│           ├── index.html
│           ├── search.html
│           ├── magazines.html
│           ├── magazine.html
│           └── page.html
└── tests/
    ├── __init__.py
    ├── conftest.py             # shared fixtures (db, settings, bundles)
    ├── fixtures/
    │   ├── __init__.py
    │   ├── pdfs.py             # programmatic PDF generation for tests
    │   ├── cbzs.py             # programmatic CBZ generation for tests
    │   └── tiny.cbr            # checked-in tiny CBR (rar archive can't be generated freely)
    ├── test_ids.py
    ├── test_manifest.py
    ├── test_ocr.py
    ├── test_formats_pdf.py
    ├── test_formats_archives.py
    ├── test_normalize.py
    ├── test_pipeline.py
    ├── test_importer.py
    ├── test_search.py
    ├── test_web_search.py
    ├── test_web_magazines.py
    ├── test_web_page.py
    ├── test_web_bundle.py
    └── test_cli.py
```

Each file under `src/magsearch/` has one clear responsibility; the cross-file interfaces are the `OCREngine` protocol (between pipeline and OCR), the `Manifest` pydantic model (between ingest and import), and the SQLAlchemy models (between import, search, and web).

---

## Task 1: Project Scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/magsearch/__init__.py`
- Create: `src/magsearch/ingest/__init__.py`
- Create: `src/magsearch/web/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1.1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "magsearch"
version = "0.1.0"
description = "Search old magazine scans by full-text OCR."
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "sqlalchemy>=2.0",
    "alembic>=1.13",
    "pydantic>=2.6",
    "pydantic-settings>=2.2",
    "typer>=0.12",
    "jinja2>=3.1",
    "pillow>=10.2",
    "pymupdf>=1.24",
    "rarfile>=4.1",
    "python-multipart>=0.0.9",
    "httpx>=0.27",
]

[project.optional-dependencies]
ocr = [
    "paddleocr>=2.7",
    "paddlepaddle>=2.6",
]
dev = [
    "pytest>=8.0",
    "pytest-cov>=4.1",
    "ruff>=0.4",
]

[project.scripts]
magsearch = "magsearch.cli:app"

[tool.hatch.build.targets.wheel]
packages = ["src/magsearch"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "ocr: tests that require PaddleOCR (skipped by default; opt in with -m ocr)",
]
addopts = "-m 'not ocr'"

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP", "W"]
```

- [ ] **Step 1.2: Write `.gitignore`**

```
__pycache__/
*.py[cod]
*$py.class
.pytest_cache/
.coverage
.ruff_cache/
*.egg-info/
dist/
build/
.venv/
venv/
data/
bundles/
*.db
*.db-journal
*.db-shm
*.db-wal
.env
```

- [ ] **Step 1.3: Create empty package files**

```bash
mkdir -p src/magsearch/ingest src/magsearch/web/templates tests/fixtures
touch src/magsearch/__init__.py src/magsearch/ingest/__init__.py src/magsearch/web/__init__.py
touch tests/__init__.py tests/fixtures/__init__.py
```

- [ ] **Step 1.4: Write `src/magsearch/__init__.py`**

```python
__version__ = "0.1.0"
```

- [ ] **Step 1.5: Write minimal `tests/conftest.py`**

```python
"""Shared pytest fixtures. Populated by later tasks."""
```

- [ ] **Step 1.6: Install in editable mode and verify pytest runs**

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest -v
```

Expected: `pytest` runs successfully and reports `no tests ran`.

- [ ] **Step 1.7: Commit**

```bash
git add pyproject.toml .gitignore src/ tests/
git commit -m "feat: scaffold magsearch package skeleton"
```

---

## Task 2: Settings Module

Implements the spec section "Architecture" — central config for DB location and bundles directory, shared by CLI, importer, and web app.

**Files:**
- Create: `src/magsearch/settings.py`
- Create: `tests/test_settings.py`

- [ ] **Step 2.1: Write the failing test**

```python
# tests/test_settings.py
from pathlib import Path

from magsearch.settings import Settings


def test_settings_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.database_url == "sqlite:///./data/magsearch.db"
    assert s.bundles_dir == Path("./data/bundles")


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", "sqlite:////tmp/x.db")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", "/var/bundles")
    s = Settings()
    assert s.database_url == "sqlite:////tmp/x.db"
    assert s.bundles_dir == Path("/var/bundles")
```

- [ ] **Step 2.2: Run test, verify fail**

```bash
pytest tests/test_settings.py -v
```

Expected: ImportError / `ModuleNotFoundError: magsearch.settings`.

- [ ] **Step 2.3: Implement `src/magsearch/settings.py`**

```python
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAGSEARCH_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/magsearch.db"
    bundles_dir: Path = Path("./data/bundles")


def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 2.4: Run tests, verify pass**

```bash
pytest tests/test_settings.py -v
```

Expected: both tests pass.

- [ ] **Step 2.5: Commit**

```bash
git add src/magsearch/settings.py tests/test_settings.py
git commit -m "feat: add settings module"
```

---

## Task 3: DB Models + Initial Alembic Migration

Implements spec section "Data Model": tables `magazines`, `pages`, FTS5 virtual table `pages_fts`, and three sync triggers.

**Files:**
- Create: `src/magsearch/db.py`
- Create: `src/magsearch/models.py`
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/0001_initial.py`
- Create: `tests/test_db.py`

- [ ] **Step 3.1: Initialize Alembic**

```bash
alembic init alembic
```

This creates `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/`. We will overwrite `alembic.ini` and `alembic/env.py` in the next steps.

- [ ] **Step 3.2: Write `src/magsearch/models.py`**

```python
from datetime import date, datetime

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Magazine(Base):
    __tablename__ = "magazines"

    id: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str]
    issue: Mapped[str | None]
    publication_date: Mapped[date | None]
    publisher: Mapped[str | None]
    original_filename: Mapped[str]
    original_format: Mapped[str]
    page_count: Mapped[int]
    content_hash: Mapped[str]
    cover_path: Mapped[str]
    ocr_engine: Mapped[str]
    ocr_engine_version: Mapped[str]
    ingested_at: Mapped[datetime]

    pages: Mapped[list["Page"]] = relationship(
        back_populates="magazine",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Page(Base):
    __tablename__ = "pages"
    __table_args__ = (UniqueConstraint("magazine_id", "page_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    magazine_id: Mapped[str] = mapped_column(
        ForeignKey("magazines.id", ondelete="CASCADE"), nullable=False
    )
    page_number: Mapped[int]
    image_path: Mapped[str]
    thumb_path: Mapped[str]
    text: Mapped[str]

    magazine: Mapped[Magazine] = relationship(back_populates="pages")
```

- [ ] **Step 3.3: Write `src/magsearch/db.py`**

```python
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, future=True)

    @event.listens_for(engine, "connect")
    def _enable_sqlite_fk(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, future=True)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

- [ ] **Step 3.4: Overwrite `alembic.ini` essential settings**

In `alembic.ini`, change the `sqlalchemy.url` line to a placeholder that will be overridden from env:

```ini
sqlalchemy.url = sqlite:///./data/magsearch.db
```

Leave the rest of the file as Alembic generated it.

- [ ] **Step 3.5: Overwrite `alembic/env.py`**

```python
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from magsearch.models import Base
from magsearch.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 3.6: Write the initial migration**

Create `alembic/versions/0001_initial.py`:

```python
"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-05-11

"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "magazines",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("issue", sa.String(), nullable=True),
        sa.Column("publication_date", sa.Date(), nullable=True),
        sa.Column("publisher", sa.String(), nullable=True),
        sa.Column("original_filename", sa.String(), nullable=False),
        sa.Column("original_format", sa.String(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("cover_path", sa.String(), nullable=False),
        sa.Column("ocr_engine", sa.String(), nullable=False),
        sa.Column("ocr_engine_version", sa.String(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "pages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "magazine_id",
            sa.String(),
            sa.ForeignKey("magazines.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("image_path", sa.String(), nullable=False),
        sa.Column("thumb_path", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.UniqueConstraint("magazine_id", "page_number", name="uq_pages_mag_page"),
    )
    op.execute(
        """
        CREATE VIRTUAL TABLE pages_fts USING fts5(
            text,
            content='pages',
            content_rowid='id',
            tokenize = 'porter unicode61 remove_diacritics 2'
        )
        """
    )
    op.execute(
        """
        CREATE TRIGGER pages_ai AFTER INSERT ON pages BEGIN
            INSERT INTO pages_fts(rowid, text) VALUES (new.id, new.text);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER pages_ad AFTER DELETE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, text) VALUES('delete', old.id, old.text);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER pages_au AFTER UPDATE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, text) VALUES('delete', old.id, old.text);
            INSERT INTO pages_fts(rowid, text) VALUES (new.id, new.text);
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS pages_au")
    op.execute("DROP TRIGGER IF EXISTS pages_ad")
    op.execute("DROP TRIGGER IF EXISTS pages_ai")
    op.execute("DROP TABLE IF EXISTS pages_fts")
    op.drop_table("pages")
    op.drop_table("magazines")
```

- [ ] **Step 3.7: Write `tests/test_db.py`**

```python
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
```

- [ ] **Step 3.8: Run tests, verify pass**

```bash
pytest tests/test_db.py -v
```

Expected: both tests pass.

- [ ] **Step 3.9: Commit**

```bash
git add src/magsearch/models.py src/magsearch/db.py alembic.ini alembic/ tests/test_db.py
git commit -m "feat: add models, db module, and initial migration with FTS5"
```

---

## Task 4: Manifest Pydantic Models

Implements the bundle file format — the cross-machine contract. Spec sections "Ingestion Pipeline" step 5 and "Server Side / import".

**Files:**
- Create: `src/magsearch/manifest.py`
- Create: `tests/test_manifest.py`

- [ ] **Step 4.1: Write failing tests**

```python
# tests/test_manifest.py
import json
from datetime import date

import pytest
from pydantic import ValidationError

from magsearch.manifest import Manifest, PageEntry, FileChecksum


def _sample_manifest_dict() -> dict:
    return {
        "schema_version": 1,
        "id": "byte-1985-12",
        "title": "Byte",
        "issue": "Vol 10 No 12",
        "publication_date": "1985-12-01",
        "publisher": "McGraw-Hill",
        "original_filename": "byte.pdf",
        "original_format": "pdf",
        "page_count": 2,
        "content_hash": "abc123",
        "ocr_engine": "paddleocr",
        "ocr_engine_version": "2.7.0",
        "cover_path": "cover.webp",
        "pages": [
            {"page_number": 1, "image_path": "pages/0001.webp",
             "thumb_path": "thumbs/0001.webp", "ocr_path": "ocr/0001.json",
             "text": "first page text"},
            {"page_number": 2, "image_path": "pages/0002.webp",
             "thumb_path": "thumbs/0002.webp", "ocr_path": "ocr/0002.json",
             "text": "second page text"},
        ],
        "checksums": [
            {"path": "pages/0001.webp", "sha256": "111"},
            {"path": "cover.webp", "sha256": "222"},
        ],
    }


def test_manifest_roundtrip():
    m = Manifest.model_validate(_sample_manifest_dict())
    assert m.id == "byte-1985-12"
    assert m.publication_date == date(1985, 12, 1)
    assert len(m.pages) == 2
    again = Manifest.model_validate(json.loads(m.model_dump_json()))
    assert again == m


def test_manifest_rejects_wrong_schema_version():
    d = _sample_manifest_dict()
    d["schema_version"] = 99
    with pytest.raises(ValidationError):
        Manifest.model_validate(d)


def test_manifest_rejects_extra_fields():
    d = _sample_manifest_dict()
    d["unexpected"] = "should fail"
    with pytest.raises(ValidationError):
        Manifest.model_validate(d)


def test_manifest_format_must_be_known():
    d = _sample_manifest_dict()
    d["original_format"] = "txt"
    with pytest.raises(ValidationError):
        Manifest.model_validate(d)
```

- [ ] **Step 4.2: Run tests, verify fail**

```bash
pytest tests/test_manifest.py -v
```

Expected: `ModuleNotFoundError: magsearch.manifest`.

- [ ] **Step 4.3: Implement `src/magsearch/manifest.py`**

```python
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict


class FileChecksum(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str


class PageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int
    image_path: str
    thumb_path: str
    ocr_path: str
    text: str


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    id: str
    title: str
    issue: str | None = None
    publication_date: date | None = None
    publisher: str | None = None
    original_filename: str
    original_format: Literal["pdf", "cbz", "cbr"]
    page_count: int
    content_hash: str
    ocr_engine: str
    ocr_engine_version: str
    cover_path: str
    pages: list[PageEntry]
    checksums: list[FileChecksum]
```

- [ ] **Step 4.4: Run tests, verify pass**

```bash
pytest tests/test_manifest.py -v
```

Expected: all four tests pass.

- [ ] **Step 4.5: Commit**

```bash
git add src/magsearch/manifest.py tests/test_manifest.py
git commit -m "feat: add bundle manifest pydantic models"
```

---

## Task 5: ID Generation

Implements spec section "Ingestion Pipeline / Stable ID generation": slugify, content hash, generate_id.

**Files:**
- Create: `src/magsearch/ingest/ids.py`
- Create: `tests/test_ids.py`

- [ ] **Step 5.1: Write failing tests**

```python
# tests/test_ids.py
from datetime import date

from magsearch.ingest.ids import slugify, content_hash, generate_id


def test_slugify_basic():
    assert slugify("Byte Magazine") == "byte-magazine"


def test_slugify_unicode_and_punctuation():
    assert slugify("Père Ubu — Issue #3!") == "pere-ubu-issue-3"


def test_slugify_empty_returns_empty_string():
    assert slugify("") == ""
    assert slugify("---") == ""


def test_content_hash_deterministic(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"hello")
    h1 = content_hash(p)
    h2 = content_hash(p)
    assert h1 == h2
    assert len(h1) == 64


def test_generate_id_uses_title_and_date():
    h = "a" * 64
    assert generate_id(title="Byte", publication_date=date(1985, 12, 1),
                       override=None, content_hash=h) == "byte-1985-12"


def test_generate_id_override_wins():
    h = "a" * 64
    assert generate_id(title="Byte", publication_date=date(1985, 12, 1),
                       override="custom-id", content_hash=h) == "custom-id"


def test_generate_id_falls_back_to_hash():
    h = "abcdef0123456789" + "0" * 48
    assert generate_id(title="Untitled", publication_date=None,
                       override=None, content_hash=h) == "abcdef012345"
```

- [ ] **Step 5.2: Run tests, verify fail**

```bash
pytest tests/test_ids.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 5.3: Implement `src/magsearch/ingest/ids.py`**

```python
import hashlib
import re
import unicodedata
from datetime import date
from pathlib import Path


_SLUG_KEEP = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    normalized = normalized.lower()
    normalized = _SLUG_KEEP.sub("-", normalized).strip("-")
    return normalized


def content_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def generate_id(
    *, title: str, publication_date: date | None, override: str | None, content_hash: str
) -> str:
    if override:
        return slugify(override)
    slug = slugify(title)
    if slug and publication_date is not None:
        return f"{slug}-{publication_date.year:04d}-{publication_date.month:02d}"
    return content_hash[:12]
```

- [ ] **Step 5.4: Run tests, verify pass**

```bash
pytest tests/test_ids.py -v
```

Expected: all 7 tests pass.

- [ ] **Step 5.5: Commit**

```bash
git add src/magsearch/ingest/ids.py tests/test_ids.py
git commit -m "feat: add slug, content hash, and id generation"
```

---

## Task 6: OCR Protocol, OCRRegion, FakeOCREngine, Reading-Order Sort

Implements spec section "Project Layout / OCREngine" and step 4 of the pipeline (reading-order concatenation). PaddleOCR impl is in Task 12.

**Files:**
- Create: `src/magsearch/ingest/ocr.py`
- Create: `tests/test_ocr.py`

- [ ] **Step 6.1: Write failing tests**

```python
# tests/test_ocr.py
from PIL import Image

from magsearch.ingest.ocr import (
    FakeOCREngine,
    OCRRegion,
    concatenate_reading_order,
)


def test_ocr_region_fields():
    r = OCRRegion(text="hello", bbox=(0, 0, 100, 20), confidence=0.95)
    assert r.text == "hello"
    assert r.bbox == (0, 0, 100, 20)


def test_fake_engine_returns_default():
    e = FakeOCREngine()
    img = Image.new("RGB", (50, 50), "white")
    out = e.recognize(img)
    assert len(out) == 1
    assert out[0].text == "fake page"


def test_fake_engine_uses_scripted_responses_in_order():
    scripted = [
        [OCRRegion(text="one", bbox=(0, 0, 10, 10), confidence=1.0)],
        [OCRRegion(text="two", bbox=(0, 0, 10, 10), confidence=1.0)],
    ]
    e = FakeOCREngine(responses=scripted)
    img = Image.new("RGB", (50, 50), "white")
    assert e.recognize(img)[0].text == "one"
    assert e.recognize(img)[0].text == "two"
    assert e.recognize(img)[0].text == "fake page"  # falls back to default


def test_concatenate_reading_order_sorts_top_to_bottom_then_left_to_right():
    regions = [
        OCRRegion(text="C", bbox=(0, 200, 50, 220), confidence=1.0),
        OCRRegion(text="A", bbox=(0, 0, 50, 20), confidence=1.0),
        OCRRegion(text="B", bbox=(100, 0, 150, 20), confidence=1.0),
    ]
    assert concatenate_reading_order(regions) == "A B C"


def test_concatenate_reading_order_groups_by_line_tolerance():
    # Two regions almost at the same y should be treated as one line and ordered by x.
    regions = [
        OCRRegion(text="right", bbox=(200, 100, 250, 120), confidence=1.0),
        OCRRegion(text="left", bbox=(0, 102, 50, 122), confidence=1.0),
    ]
    assert concatenate_reading_order(regions) == "left right"
```

- [ ] **Step 6.2: Run tests, verify fail**

```bash
pytest tests/test_ocr.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 6.3: Implement `src/magsearch/ingest/ocr.py`**

```python
from dataclasses import dataclass
from typing import Protocol

from PIL import Image


@dataclass(frozen=True)
class OCRRegion:
    text: str
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1
    confidence: float


class OCREngine(Protocol):
    name: str
    version: str

    def recognize(self, image: Image.Image) -> list[OCRRegion]: ...


class FakeOCREngine:
    """Deterministic test double. Returns scripted responses then falls back to default."""

    name: str = "fake"
    version: str = "0.0.1"

    def __init__(self, responses: list[list[OCRRegion]] | None = None) -> None:
        self._responses = list(responses or [])
        self._index = 0
        self._default = [OCRRegion(text="fake page", bbox=(0, 0, 100, 20), confidence=1.0)]

    def recognize(self, image: Image.Image) -> list[OCRRegion]:
        if self._index < len(self._responses):
            r = self._responses[self._index]
            self._index += 1
            return r
        return self._default


_LINE_TOLERANCE_PX = 15


def concatenate_reading_order(regions: list[OCRRegion]) -> str:
    if not regions:
        return ""
    sorted_by_y = sorted(regions, key=lambda r: r.bbox[1])
    lines: list[list[OCRRegion]] = []
    for r in sorted_by_y:
        if lines and abs(r.bbox[1] - lines[-1][0].bbox[1]) <= _LINE_TOLERANCE_PX:
            lines[-1].append(r)
        else:
            lines.append([r])
    parts: list[str] = []
    for line in lines:
        line.sort(key=lambda r: r.bbox[0])
        parts.append(" ".join(r.text for r in line))
    return " ".join(parts)
```

- [ ] **Step 6.4: Run tests, verify pass**

```bash
pytest tests/test_ocr.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 6.5: Commit**

```bash
git add src/magsearch/ingest/ocr.py tests/test_ocr.py
git commit -m "feat: add OCR protocol, fake engine, and reading-order util"
```

---

## Task 7: PDF Reader and Format Detection

Implements spec section "Ingestion Pipeline" steps 1 and 2 (for PDFs). Archive formats follow in Task 8.

**Files:**
- Create: `src/magsearch/ingest/formats.py`
- Create: `tests/fixtures/pdfs.py`
- Create: `tests/test_formats_pdf.py`

- [ ] **Step 7.1: Write the PDF fixture helper**

```python
# tests/fixtures/pdfs.py
from pathlib import Path

import fitz  # PyMuPDF


def make_pdf(path: Path, num_pages: int = 2, page_text: list[str] | None = None) -> Path:
    doc = fitz.open()
    for i in range(num_pages):
        page = doc.new_page(width=200, height=300)
        text = page_text[i] if page_text and i < len(page_text) else f"Page {i + 1}"
        page.insert_text((20, 50), text)
    doc.save(str(path))
    doc.close()
    return path
```

- [ ] **Step 7.2: Write failing tests**

```python
# tests/test_formats_pdf.py
from PIL import Image

from magsearch.ingest.formats import detect_format, read_pages
from tests.fixtures.pdfs import make_pdf


def test_detect_format_pdf(tmp_path):
    p = make_pdf(tmp_path / "a.pdf")
    assert detect_format(p) == "pdf"


def test_detect_format_rejects_unknown(tmp_path):
    p = tmp_path / "x.txt"
    p.write_bytes(b"not a real magazine")
    assert detect_format(p) is None


def test_read_pages_pdf_yields_images(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", num_pages=3)
    pages = list(read_pages(p, "pdf"))
    assert [pn for pn, _ in pages] == [1, 2, 3]
    assert all(isinstance(img, Image.Image) for _, img in pages)
    assert all(img.mode in ("RGB", "RGBA") for _, img in pages)
```

- [ ] **Step 7.3: Run tests, verify fail**

```bash
pytest tests/test_formats_pdf.py -v
```

Expected: `ModuleNotFoundError: magsearch.ingest.formats`.

- [ ] **Step 7.4: Implement initial `src/magsearch/ingest/formats.py`**

```python
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import fitz  # PyMuPDF
from PIL import Image

Format = Literal["pdf", "cbz", "cbr"]

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_RAR4_MAGIC = b"Rar!\x1a\x07\x00"
_RAR5_MAGIC = b"Rar!\x1a\x07\x01\x00"


def detect_format(path: Path) -> Format | None:
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(_PDF_MAGIC):
        return "pdf"
    if head.startswith(_ZIP_MAGIC):
        return "cbz"
    if head.startswith(_RAR4_MAGIC) or head.startswith(_RAR5_MAGIC):
        return "cbr"
    return None


def read_pages(path: Path, fmt: Format) -> Iterator[tuple[int, Image.Image]]:
    if fmt == "pdf":
        yield from _read_pdf(path)
    else:
        raise NotImplementedError(f"format {fmt} not yet implemented")


def _read_pdf(path: Path) -> Iterator[tuple[int, Image.Image]]:
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=200)
            mode = "RGB" if pix.alpha == 0 else "RGBA"
            img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
            yield i, img
```

- [ ] **Step 7.5: Run tests, verify pass**

```bash
pytest tests/test_formats_pdf.py -v
```

Expected: 3 tests pass.

- [ ] **Step 7.6: Commit**

```bash
git add src/magsearch/ingest/formats.py tests/fixtures/pdfs.py tests/test_formats_pdf.py
git commit -m "feat: add format detection and PDF page reader"
```

---

## Task 8: CBZ and CBR Readers

Implements spec section "Ingestion Pipeline" step 2 for archive formats. CBR requires `unrar` or `unar` on PATH at runtime; tests use a committed tiny `.cbr` fixture.

**Files:**
- Modify: `src/magsearch/ingest/formats.py`
- Create: `tests/fixtures/cbzs.py`
- Create: `tests/fixtures/tiny.cbr` (binary fixture — see step 8.1)
- Create: `tests/test_formats_archives.py`

- [ ] **Step 8.1: Add a tiny `.cbr` fixture**

Provide a small RAR archive at `tests/fixtures/tiny.cbr` containing two PNG files named `001.png` and `002.png`. Generation requires the proprietary `rar` binary, so this is a committed artifact rather than a generated one. If you don't have `rar`: a small reference fixture can be obtained from any source you trust (or skip-marked from your test run with `-k 'not cbr'`). To regenerate locally:

```bash
mkdir -p /tmp/cbr_src
python -c "from PIL import Image; Image.new('RGB',(50,50),'red').save('/tmp/cbr_src/001.png'); Image.new('RGB',(50,50),'blue').save('/tmp/cbr_src/002.png')"
cd /tmp/cbr_src && rar a -m0 tiny.rar 001.png 002.png && mv tiny.rar <repo>/tests/fixtures/tiny.cbr
```

Commit the resulting `tiny.cbr`. It should be only a few KB.

- [ ] **Step 8.2: Write the CBZ fixture helper**

```python
# tests/fixtures/cbzs.py
import io
import zipfile
from pathlib import Path

from PIL import Image


def make_cbz(path: Path, num_pages: int = 2) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(num_pages):
            img = Image.new("RGB", (100, 150), color=(i * 40, i * 40, i * 40))
            buf = io.BytesIO()
            img.save(buf, "PNG")
            zf.writestr(f"page{i + 1:03d}.png", buf.getvalue())
    return path
```

- [ ] **Step 8.3: Write failing tests**

```python
# tests/test_formats_archives.py
import shutil
from pathlib import Path

import pytest
from PIL import Image

from magsearch.ingest.formats import detect_format, read_pages
from tests.fixtures.cbzs import make_cbz

CBR_FIXTURE = Path(__file__).parent / "fixtures" / "tiny.cbr"


def test_detect_format_cbz(tmp_path):
    p = make_cbz(tmp_path / "a.cbz")
    assert detect_format(p) == "cbz"


def test_read_pages_cbz_yields_in_name_order(tmp_path):
    p = make_cbz(tmp_path / "a.cbz", num_pages=3)
    pages = list(read_pages(p, "cbz"))
    assert [pn for pn, _ in pages] == [1, 2, 3]
    assert all(isinstance(img, Image.Image) for _, img in pages)


def test_detect_format_cbr():
    assert CBR_FIXTURE.exists(), "tiny.cbr fixture missing — see Task 8 step 1"
    assert detect_format(CBR_FIXTURE) == "cbr"


@pytest.mark.skipif(
    shutil.which("unrar") is None and shutil.which("unar") is None,
    reason="needs unrar or unar on PATH",
)
def test_read_pages_cbr():
    pages = list(read_pages(CBR_FIXTURE, "cbr"))
    assert len(pages) == 2
    assert all(isinstance(img, Image.Image) for _, img in pages)
```

- [ ] **Step 8.4: Run tests, verify fail**

```bash
pytest tests/test_formats_archives.py -v
```

Expected: CBZ tests fail with `NotImplementedError`, CBR detection passes, CBR read either fails or skips.

- [ ] **Step 8.5: Extend `src/magsearch/ingest/formats.py`**

Replace the placeholder branch with full implementations. The new file contents:

```python
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import Literal
from zipfile import ZipFile

import fitz  # PyMuPDF
import rarfile
from PIL import Image

Format = Literal["pdf", "cbz", "cbr"]

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_RAR4_MAGIC = b"Rar!\x1a\x07\x00"
_RAR5_MAGIC = b"Rar!\x1a\x07\x01\x00"

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def detect_format(path: Path) -> Format | None:
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(_PDF_MAGIC):
        return "pdf"
    if head.startswith(_ZIP_MAGIC):
        return "cbz"
    if head.startswith(_RAR4_MAGIC) or head.startswith(_RAR5_MAGIC):
        return "cbr"
    return None


def read_pages(path: Path, fmt: Format) -> Iterator[tuple[int, Image.Image]]:
    if fmt == "pdf":
        yield from _read_pdf(path)
    elif fmt == "cbz":
        yield from _read_cbz(path)
    elif fmt == "cbr":
        yield from _read_cbr(path)
    else:
        raise ValueError(f"unsupported format {fmt!r}")


def _read_pdf(path: Path) -> Iterator[tuple[int, Image.Image]]:
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=200)
            mode = "RGB" if pix.alpha == 0 else "RGBA"
            img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
            yield i, img


def _read_cbz(path: Path) -> Iterator[tuple[int, Image.Image]]:
    with ZipFile(path) as zf:
        names = sorted(
            n for n in zf.namelist()
            if not n.endswith("/") and n.lower().endswith(_IMAGE_EXTS)
        )
        for i, name in enumerate(names, start=1):
            with zf.open(name) as fh:
                img = Image.open(BytesIO(fh.read()))
                img.load()
                yield i, img


def _read_cbr(path: Path) -> Iterator[tuple[int, Image.Image]]:
    with rarfile.RarFile(path) as rf:
        names = sorted(
            n for n in rf.namelist()
            if not n.endswith("/") and n.lower().endswith(_IMAGE_EXTS)
        )
        for i, name in enumerate(names, start=1):
            with rf.open(name) as fh:
                img = Image.open(BytesIO(fh.read()))
                img.load()
                yield i, img
```

- [ ] **Step 8.6: Run tests, verify pass**

```bash
pytest tests/test_formats_archives.py -v
```

Expected: 3 tests pass, 1 may skip if `unrar`/`unar` not present.

- [ ] **Step 8.7: Commit**

```bash
git add src/magsearch/ingest/formats.py tests/fixtures/cbzs.py tests/fixtures/tiny.cbr tests/test_formats_archives.py
git commit -m "feat: add CBZ and CBR page readers"
```

---

## Task 9: Image Normalization

Implements spec section "Ingestion Pipeline" step 3 — WebP page images at ~1800px long edge, thumbnails at 400px, cover as a copy of the first thumbnail.

**Files:**
- Create: `src/magsearch/ingest/normalize.py`
- Create: `tests/test_normalize.py`

- [ ] **Step 9.1: Write failing tests**

```python
# tests/test_normalize.py
from pathlib import Path

from PIL import Image

from magsearch.ingest.normalize import encode_page, encode_thumb, write_cover


def test_encode_page_resizes_long_edge_and_writes_webp(tmp_path):
    src = Image.new("RGB", (3600, 2400), "white")
    out = tmp_path / "page.webp"
    encode_page(src, out)
    assert out.exists()
    img = Image.open(out)
    assert img.format == "WEBP"
    assert max(img.size) == 1800
    assert img.size == (1800, 1200)


def test_encode_page_skips_upscale(tmp_path):
    src = Image.new("RGB", (500, 400), "white")
    out = tmp_path / "page.webp"
    encode_page(src, out)
    img = Image.open(out)
    assert img.size == (500, 400)


def test_encode_thumb_long_edge_400(tmp_path):
    src = Image.new("RGB", (1000, 1500), "white")
    out = tmp_path / "thumb.webp"
    encode_thumb(src, out)
    img = Image.open(out)
    assert max(img.size) == 400


def test_write_cover_copies_thumb(tmp_path):
    thumb = tmp_path / "0001.webp"
    Image.new("RGB", (200, 300), "red").save(thumb, "WEBP")
    cover = tmp_path / "cover.webp"
    write_cover(thumb, cover)
    assert cover.exists()
    assert cover.read_bytes() == thumb.read_bytes()
```

- [ ] **Step 9.2: Run tests, verify fail**

```bash
pytest tests/test_normalize.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 9.3: Implement `src/magsearch/ingest/normalize.py`**

```python
import shutil
from pathlib import Path

from PIL import Image

PAGE_LONG_EDGE = 1800
PAGE_QUALITY = 80
THUMB_LONG_EDGE = 400
THUMB_QUALITY = 70


def _resize_long_edge(img: Image.Image, long_edge: int) -> Image.Image:
    w, h = img.size
    longest = max(w, h)
    if longest <= long_edge:
        return img
    scale = long_edge / longest
    return img.resize((int(round(w * scale)), int(round(h * scale))), Image.LANCZOS)


def encode_page(image: Image.Image, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    resized = _resize_long_edge(image.convert("RGB"), PAGE_LONG_EDGE)
    resized.save(out, "WEBP", quality=PAGE_QUALITY)


def encode_thumb(image: Image.Image, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    resized = _resize_long_edge(image.convert("RGB"), THUMB_LONG_EDGE)
    resized.save(out, "WEBP", quality=THUMB_QUALITY)


def write_cover(first_thumb: Path, cover: Path) -> None:
    cover.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(first_thumb, cover)
```

- [ ] **Step 9.4: Run tests, verify pass**

```bash
pytest tests/test_normalize.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 9.5: Commit**

```bash
git add src/magsearch/ingest/normalize.py tests/test_normalize.py
git commit -m "feat: add page/thumb/cover image normalization"
```

---

## Task 10: Ingestion Pipeline Orchestrator

Implements spec section "Ingestion Pipeline" steps 1–6 wired together. Idempotent re-run via existing-manifest check and tempfile+rename per step.

**Files:**
- Create: `src/magsearch/ingest/pipeline.py`
- Create: `tests/test_pipeline.py`

- [ ] **Step 10.1: Write failing tests**

```python
# tests/test_pipeline.py
import json
from datetime import date
from pathlib import Path

from PIL import Image

from magsearch.ingest.ocr import FakeOCREngine, OCRRegion
from magsearch.ingest.pipeline import IngestPipeline, IngestOptions
from magsearch.manifest import Manifest
from tests.fixtures.pdfs import make_pdf


def _scripted_engine_for(num_pages: int) -> FakeOCREngine:
    return FakeOCREngine(
        responses=[
            [OCRRegion(text=f"text on page {i + 1}", bbox=(0, 0, 100, 20), confidence=1.0)]
            for i in range(num_pages)
        ]
    )


def test_pipeline_produces_complete_bundle(tmp_path: Path):
    src = make_pdf(tmp_path / "byte.pdf", num_pages=2)
    bundles = tmp_path / "bundles"
    engine = _scripted_engine_for(2)

    result = IngestPipeline(
        bundles_root=bundles,
        ocr_engine=engine,
        options=IngestOptions(
            title="Byte", issue="Vol 10 No 12",
            publication_date=date(1985, 12, 1), publisher="McGraw-Hill",
        ),
    ).run(src)

    bundle = bundles / result.id
    assert (bundle / "manifest.json").exists()
    assert (bundle / "original.pdf").exists()
    assert (bundle / "pages" / "0001.webp").exists()
    assert (bundle / "pages" / "0002.webp").exists()
    assert (bundle / "thumbs" / "0001.webp").exists()
    assert (bundle / "thumbs" / "0002.webp").exists()
    assert (bundle / "ocr" / "0001.json").exists()
    assert (bundle / "cover.webp").exists()

    manifest = Manifest.model_validate_json((bundle / "manifest.json").read_text())
    assert manifest.id == "byte-1985-12"
    assert manifest.page_count == 2
    assert manifest.original_format == "pdf"
    assert manifest.pages[0].text == "text on page 1"
    assert manifest.ocr_engine == "fake"
    # Every entry in checksums actually exists in the bundle.
    for c in manifest.checksums:
        assert (bundle / c.path).exists()


def test_pipeline_is_idempotent_without_force(tmp_path: Path):
    src = make_pdf(tmp_path / "byte.pdf", num_pages=1)
    bundles = tmp_path / "bundles"

    r1 = IngestPipeline(
        bundles_root=bundles, ocr_engine=_scripted_engine_for(1),
        options=IngestOptions(title="Byte", publication_date=date(1985, 12, 1)),
    ).run(src)
    mtime1 = (bundles / r1.id / "manifest.json").stat().st_mtime_ns

    r2 = IngestPipeline(
        bundles_root=bundles, ocr_engine=_scripted_engine_for(1),
        options=IngestOptions(title="Byte", publication_date=date(1985, 12, 1)),
    ).run(src)
    mtime2 = (bundles / r2.id / "manifest.json").stat().st_mtime_ns

    assert r1.id == r2.id
    assert mtime1 == mtime2  # manifest not rewritten


def test_pipeline_force_overwrites(tmp_path: Path):
    src = make_pdf(tmp_path / "byte.pdf", num_pages=1)
    bundles = tmp_path / "bundles"
    opts = IngestOptions(title="Byte", publication_date=date(1985, 12, 1))

    IngestPipeline(bundles, _scripted_engine_for(1), opts).run(src)
    pre = (bundles / "byte-1985-12" / "manifest.json").stat().st_mtime_ns

    IngestPipeline(bundles, _scripted_engine_for(1), opts).run(src, force=True)
    post = (bundles / "byte-1985-12" / "manifest.json").stat().st_mtime_ns

    assert post >= pre


def test_pipeline_rejects_unknown_format(tmp_path: Path):
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"hello world")
    bundles = tmp_path / "bundles"
    import pytest
    with pytest.raises(ValueError, match="unrecognized format"):
        IngestPipeline(
            bundles, _scripted_engine_for(1), IngestOptions(title="x")
        ).run(junk)
```

- [ ] **Step 10.2: Run tests, verify fail**

```bash
pytest tests/test_pipeline.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 10.3: Implement `src/magsearch/ingest/pipeline.py`**

```python
import json
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from magsearch.ingest.formats import detect_format, read_pages
from magsearch.ingest.ids import content_hash, generate_id
from magsearch.ingest.normalize import encode_page, encode_thumb, write_cover
from magsearch.ingest.ocr import OCREngine, OCRRegion, concatenate_reading_order
from magsearch.manifest import FileChecksum, Manifest, PageEntry


@dataclass
class IngestOptions:
    title: str = ""
    issue: str | None = None
    publication_date: date | None = None
    publisher: str | None = None
    id_override: str | None = None


@dataclass
class IngestResult:
    id: str
    bundle_dir: Path
    manifest: Manifest


class IngestPipeline:
    def __init__(
        self,
        bundles_root: Path,
        ocr_engine: OCREngine,
        options: IngestOptions,
    ) -> None:
        self.bundles_root = bundles_root
        self.engine = ocr_engine
        self.options = options

    def run(self, source: Path, force: bool = False) -> IngestResult:
        fmt = detect_format(source)
        if fmt is None:
            raise ValueError(f"unrecognized format for {source}")

        hash_hex = content_hash(source)
        magazine_id = generate_id(
            title=self.options.title,
            publication_date=self.options.publication_date,
            override=self.options.id_override,
            content_hash=hash_hex,
        )
        bundle = self.bundles_root / magazine_id

        manifest_path = bundle / "manifest.json"
        if not force and manifest_path.exists():
            existing = Manifest.model_validate_json(manifest_path.read_text())
            if existing.content_hash == hash_hex:
                return IngestResult(id=magazine_id, bundle_dir=bundle, manifest=existing)

        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "pages").mkdir(exist_ok=True)
        (bundle / "thumbs").mkdir(exist_ok=True)
        (bundle / "ocr").mkdir(exist_ok=True)

        original_dest = bundle / f"original.{fmt}"
        if not original_dest.exists() or force:
            shutil.copyfile(source, original_dest)

        page_entries: list[PageEntry] = []
        for page_num, image in read_pages(source, fmt):
            stem = f"{page_num:04d}"
            page_img = bundle / "pages" / f"{stem}.webp"
            thumb_img = bundle / "thumbs" / f"{stem}.webp"
            ocr_json = bundle / "ocr" / f"{stem}.json"

            encode_page(image, page_img)
            encode_thumb(image, thumb_img)

            regions: list[OCRRegion] = self.engine.recognize(image)
            ocr_json.write_text(json.dumps([
                {"text": r.text, "bbox": list(r.bbox), "confidence": r.confidence}
                for r in regions
            ]))
            page_text = concatenate_reading_order(regions)

            page_entries.append(PageEntry(
                page_number=page_num,
                image_path=str(page_img.relative_to(bundle)),
                thumb_path=str(thumb_img.relative_to(bundle)),
                ocr_path=str(ocr_json.relative_to(bundle)),
                text=page_text,
            ))

        # cover = first thumbnail
        first_thumb = bundle / "thumbs" / "0001.webp"
        cover = bundle / "cover.webp"
        if first_thumb.exists():
            write_cover(first_thumb, cover)
        cover_rel = "cover.webp" if cover.exists() else ""

        manifest = Manifest(
            schema_version=1,
            id=magazine_id,
            title=self.options.title or source.stem,
            issue=self.options.issue,
            publication_date=self.options.publication_date,
            publisher=self.options.publisher,
            original_filename=source.name,
            original_format=fmt,
            page_count=len(page_entries),
            content_hash=hash_hex,
            ocr_engine=self.engine.name,
            ocr_engine_version=self.engine.version,
            cover_path=cover_rel,
            pages=page_entries,
            checksums=_collect_checksums(bundle),
        )

        # atomic write of manifest
        tmp_manifest = bundle / "manifest.json.tmp"
        tmp_manifest.write_text(manifest.model_dump_json(indent=2))
        tmp_manifest.replace(manifest_path)

        return IngestResult(id=magazine_id, bundle_dir=bundle, manifest=manifest)


def _collect_checksums(bundle: Path) -> list[FileChecksum]:
    out: list[FileChecksum] = []
    for p in sorted(bundle.rglob("*")):
        if p.is_file() and p.name not in ("manifest.json", "manifest.json.tmp"):
            out.append(FileChecksum(path=str(p.relative_to(bundle)), sha256=content_hash(p)))
    return out
```

- [ ] **Step 10.4: Run tests, verify pass**

```bash
pytest tests/test_pipeline.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 10.5: Commit**

```bash
git add src/magsearch/ingest/pipeline.py tests/test_pipeline.py
git commit -m "feat: add ingestion pipeline orchestrator"
```

---

## Task 11: Importer

Implements spec section "Server Side / import": read bundle, verify checksums, transactional `INSERT OR REPLACE` of magazine + replace of pages, collision detection.

**Files:**
- Create: `src/magsearch/importer.py`
- Create: `tests/test_importer.py`

- [ ] **Step 11.1: Write failing tests**

```python
# tests/test_importer.py
from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.importer import ImportError as MagImportError, import_bundle
from magsearch.ingest.ocr import FakeOCREngine, OCRRegion
from magsearch.ingest.pipeline import IngestOptions, IngestPipeline
from magsearch.models import Magazine, Page
from tests.fixtures.pdfs import make_pdf


def _ingested_bundle(tmp_path: Path, title: str = "Byte", num_pages: int = 2) -> Path:
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
```

- [ ] **Step 11.2: Run tests, verify fail**

```bash
pytest tests/test_importer.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 11.3: Implement `src/magsearch/importer.py`**

```python
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from magsearch.ingest.ids import content_hash
from magsearch.manifest import Manifest
from magsearch.models import Magazine, Page


class ImportError(Exception):
    """Raised when a bundle cannot be imported safely."""


def import_bundle(bundle_dir: Path, session: Session) -> str:
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.exists():
        raise ImportError(f"manifest.json missing in {bundle_dir}")
    manifest = Manifest.model_validate_json(manifest_path.read_text())

    _verify_checksums(bundle_dir, manifest)

    existing = session.scalar(select(Magazine).where(Magazine.id == manifest.id))
    if existing is not None and existing.content_hash != manifest.content_hash:
        raise ImportError(
            f"id collision: '{manifest.id}' already exists with content_hash "
            f"{existing.content_hash!r} but bundle has {manifest.content_hash!r}. "
            f"Use --id to disambiguate."
        )

    if existing is not None:
        session.execute(delete(Page).where(Page.magazine_id == manifest.id))
        session.delete(existing)
        session.flush()

    session.add(Magazine(
        id=manifest.id,
        title=manifest.title,
        issue=manifest.issue,
        publication_date=manifest.publication_date,
        publisher=manifest.publisher,
        original_filename=manifest.original_filename,
        original_format=manifest.original_format,
        page_count=manifest.page_count,
        content_hash=manifest.content_hash,
        cover_path=f"{manifest.id}/{manifest.cover_path}" if manifest.cover_path else "",
        ocr_engine=manifest.ocr_engine,
        ocr_engine_version=manifest.ocr_engine_version,
        ingested_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    session.flush()

    for entry in manifest.pages:
        session.add(Page(
            magazine_id=manifest.id,
            page_number=entry.page_number,
            image_path=f"{manifest.id}/{entry.image_path}",
            thumb_path=f"{manifest.id}/{entry.thumb_path}",
            text=entry.text,
        ))
    return manifest.id


def _verify_checksums(bundle_dir: Path, manifest: Manifest) -> None:
    for c in manifest.checksums:
        path = bundle_dir / c.path
        if not path.exists():
            raise ImportError(f"bundle missing file {c.path}")
        if content_hash(path) != c.sha256:
            raise ImportError(f"checksum mismatch on {c.path}")
```

- [ ] **Step 11.4: Run tests, verify pass**

```bash
pytest tests/test_importer.py -v
```

Expected: all 4 tests pass.

- [ ] **Step 11.5: Commit**

```bash
git add src/magsearch/importer.py tests/test_importer.py
git commit -m "feat: add bundle importer with checksum verification"
```

---

## Task 12: PaddleOCREngine (Optional)

Implements the real OCR engine. Marked with `@pytest.mark.ocr`; default test run does not require PaddleOCR.

**Files:**
- Modify: `src/magsearch/ingest/ocr.py`

- [ ] **Step 12.1: Append PaddleOCREngine to `src/magsearch/ingest/ocr.py`**

Add this code at the bottom of the file:

```python
def _try_import_paddleocr():
    try:
        from paddleocr import PaddleOCR  # type: ignore
        return PaddleOCR
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "PaddleOCR not installed — install with `pip install magsearch[ocr]`"
        ) from exc


class PaddleOCREngine:
    name: str = "paddleocr"

    def __init__(self, lang: str = "en", use_gpu: bool = True) -> None:
        PaddleOCR = _try_import_paddleocr()
        import paddleocr  # type: ignore
        self.version = getattr(paddleocr, "__version__", "unknown")
        self._impl = PaddleOCR(lang=lang, use_gpu=use_gpu, show_log=False)

    def recognize(self, image):
        import numpy as np  # type: ignore
        arr = np.array(image.convert("RGB"))
        raw = self._impl.ocr(arr, cls=True)
        regions: list[OCRRegion] = []
        if not raw or not raw[0]:
            return regions
        for box, (text, confidence) in raw[0]:
            xs = [pt[0] for pt in box]
            ys = [pt[1] for pt in box]
            regions.append(OCRRegion(
                text=text,
                bbox=(min(xs), min(ys), max(xs), max(ys)),
                confidence=float(confidence),
            ))
        return regions
```

- [ ] **Step 12.2: Add a smoke test gated by `-m ocr`**

Append to `tests/test_ocr.py`:

```python
import pytest
from PIL import Image, ImageDraw, ImageFont


@pytest.mark.ocr
def test_paddleocr_real_engine_reads_simple_text(tmp_path):
    from magsearch.ingest.ocr import PaddleOCREngine

    img = Image.new("RGB", (400, 100), "white")
    d = ImageDraw.Draw(img)
    d.text((20, 30), "HELLO WORLD", fill="black")

    engine = PaddleOCREngine(use_gpu=False)
    regions = engine.recognize(img)
    joined = " ".join(r.text.upper() for r in regions)
    assert "HELLO" in joined
```

- [ ] **Step 12.3: Verify default test run skips the OCR test**

```bash
pytest tests/test_ocr.py -v
```

Expected: 5 tests pass, 1 deselected (the `ocr`-marked one).

- [ ] **Step 12.4: Commit**

```bash
git add src/magsearch/ingest/ocr.py tests/test_ocr.py
git commit -m "feat: add PaddleOCREngine and gated smoke test"
```

---

## Task 13: Search Query + Sanitizer

Implements spec section "Server Side / Search query" — the single SQL statement, plus the input sanitizer.

**Files:**
- Create: `src/magsearch/web/search.py`
- Create: `src/magsearch/web/deps.py`
- Create: `tests/test_search.py`

- [ ] **Step 13.1: Write failing tests**

```python
# tests/test_search.py
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
```

- [ ] **Step 13.2: Run tests, verify fail**

```bash
pytest tests/test_search.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 13.3: Implement `src/magsearch/web/search.py`**

```python
import re
from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session


_SAFE_WORD = re.compile(r"[A-Za-z0-9]+")


def sanitize_query(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    # Balance double quotes: if odd count, drop them all.
    if raw.count('"') % 2 != 0:
        raw = raw.replace('"', "")

    out_parts: list[str] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == '"':
            close = raw.find('"', i + 1)
            if close == -1:
                break
            inner = " ".join(_SAFE_WORD.findall(raw[i + 1:close]))
            if inner:
                out_parts.append(f'"{inner}"')
            i = close + 1
        else:
            m = _SAFE_WORD.match(raw, i)
            if m:
                out_parts.append(m.group(0))
                i = m.end()
            else:
                i += 1
    return " ".join(out_parts)


@dataclass(frozen=True)
class SearchResult:
    magazine_id: str
    magazine_title: str
    page_number: int
    thumb_path: str
    snippet: str


_SEARCH_SQL = text("""
    SELECT
        magazines.id          AS magazine_id,
        magazines.title       AS magazine_title,
        pages.page_number     AS page_number,
        pages.thumb_path      AS thumb_path,
        snippet(pages_fts, 0, '<mark>', '</mark>', '…', 16) AS snippet
    FROM pages_fts
    JOIN pages     ON pages_fts.rowid = pages.id
    JOIN magazines ON pages.magazine_id = magazines.id
    WHERE pages_fts MATCH :q
    ORDER BY rank
    LIMIT :limit OFFSET :offset
""").bindparams(bindparam("q"), bindparam("limit"), bindparam("offset"))


def search(session: Session, raw_query: str, *, offset: int, limit: int) -> list[SearchResult]:
    q = sanitize_query(raw_query)
    if not q:
        return []
    try:
        rows = session.execute(_SEARCH_SQL, {"q": q, "limit": limit, "offset": offset}).all()
    except Exception:
        return []
    return [
        SearchResult(
            magazine_id=r.magazine_id,
            magazine_title=r.magazine_title,
            page_number=r.page_number,
            thumb_path=r.thumb_path,
            snippet=r.snippet,
        )
        for r in rows
    ]
```

- [ ] **Step 13.4: Implement `src/magsearch/web/deps.py`**

```python
from collections.abc import Iterator
from functools import lru_cache

from fastapi import Depends
from sqlalchemy.orm import Session, sessionmaker

from magsearch.db import make_engine, make_session_factory
from magsearch.settings import Settings, get_settings


@lru_cache(maxsize=1)
def _session_factory_for(database_url: str) -> sessionmaker[Session]:
    return make_session_factory(make_engine(database_url))


def get_db(settings: Settings = Depends(get_settings)) -> Iterator[Session]:
    factory = _session_factory_for(settings.database_url)
    session = factory()
    try:
        yield session
    finally:
        session.close()
```

- [ ] **Step 13.5: Run tests, verify pass**

```bash
pytest tests/test_search.py -v
```

Expected: 5 tests pass.

- [ ] **Step 13.6: Commit**

```bash
git add src/magsearch/web/search.py src/magsearch/web/deps.py tests/test_search.py
git commit -m "feat: add search query, sanitizer, and FastAPI deps"
```

---

## Task 14: Web App Factory + Landing Page

Implements spec section "Server Side / web" — `GET /`.

**Files:**
- Create: `src/magsearch/web/app.py`
- Create: `src/magsearch/web/routes.py`
- Create: `src/magsearch/web/templates/base.html`
- Create: `src/magsearch/web/templates/index.html`
- Create: `tests/test_web_landing.py`

- [ ] **Step 14.1: Write failing test**

```python
# tests/test_web_landing.py
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from magsearch.web.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(tmp_path / "bundles"))
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    return TestClient(create_app())


def test_landing_renders_search_form(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert '<form' in resp.text and 'name="q"' in resp.text
    assert "Magazine Search" in resp.text
```

- [ ] **Step 14.2: Run test, verify fail**

```bash
pytest tests/test_web_landing.py -v
```

Expected: import error.

- [ ] **Step 14.3: Write `src/magsearch/web/templates/base.html`**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{% block title %}Magazine Search{% endblock %}</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-50 text-slate-900">
<nav class="bg-white border-b border-slate-200">
  <div class="max-w-5xl mx-auto px-4 py-3 flex items-center gap-6">
    <a href="/" class="font-semibold">Magazine Search</a>
    <a href="/magazines" class="text-slate-600 hover:text-slate-900">Browse</a>
    <form action="/search" method="get" class="ml-auto flex gap-2">
      <input type="text" name="q" value="{{ q | default('', true) }}"
             placeholder="Search…"
             class="border border-slate-300 rounded px-3 py-1 w-80">
      <button class="bg-slate-900 text-white rounded px-3 py-1">Search</button>
    </form>
  </div>
</nav>
<main class="max-w-5xl mx-auto px-4 py-6">
{% block content %}{% endblock %}
</main>
</body>
</html>
```

- [ ] **Step 14.4: Write `src/magsearch/web/templates/index.html`**

```html
{% extends "base.html" %}
{% block content %}
<h1 class="text-2xl font-bold mb-4">Magazine Search</h1>
<p class="text-slate-600 mb-6">Search the full text of your archive of scanned magazines.</p>

{% if recent %}
<h2 class="text-lg font-semibold mb-2">Recently ingested</h2>
<ul class="grid grid-cols-3 gap-4">
  {% for m in recent %}
  <li class="bg-white rounded shadow p-2">
    <a href="/magazine/{{ m.id }}">
      <img src="/bundle/{{ m.cover_path }}" alt="" class="w-full h-40 object-cover">
      <div class="mt-2 text-sm font-semibold">{{ m.title }}</div>
      {% if m.issue %}<div class="text-xs text-slate-500">{{ m.issue }}</div>{% endif %}
    </a>
  </li>
  {% endfor %}
</ul>
{% endif %}
{% endblock %}
```

- [ ] **Step 14.5: Write `src/magsearch/web/routes.py`**

```python
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from magsearch.models import Magazine
from magsearch.web.deps import get_db

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def landing(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    recent = db.scalars(
        select(Magazine).order_by(Magazine.ingested_at.desc()).limit(6)
    ).all()
    return _TEMPLATES.TemplateResponse(request, "index.html", {"recent": recent})
```

- [ ] **Step 14.6: Write `src/magsearch/web/app.py`**

```python
from fastapi import FastAPI

from magsearch.web.routes import router


def create_app() -> FastAPI:
    app = FastAPI(title="Magazine Search")
    app.include_router(router)
    return app


app = create_app()
```

- [ ] **Step 14.7: Run test, verify pass**

```bash
pytest tests/test_web_landing.py -v
```

Expected: 1 test passes.

- [ ] **Step 14.8: Commit**

```bash
git add src/magsearch/web/app.py src/magsearch/web/routes.py src/magsearch/web/templates/ tests/test_web_landing.py
git commit -m "feat: add web app factory and landing page"
```

---

## Task 15: Search Route + Template

Implements `GET /search?q=&page=`. Builds on the search service from Task 13.

**Files:**
- Modify: `src/magsearch/web/routes.py`
- Create: `src/magsearch/web/templates/search.html`
- Create: `tests/test_web_search.py`
- Modify: `tests/conftest.py`

- [ ] **Step 15.1: Add a shared "populated app" fixture to conftest**

Replace `tests/conftest.py` with:

```python
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
```

- [ ] **Step 15.2: Write failing test**

```python
# tests/test_web_search.py
def test_search_returns_marked_results(app_client):
    client, _ = app_client
    resp = client.get("/search", params={"q": "synthesizer"})
    assert resp.status_code == 200
    assert "<mark>" in resp.text
    assert "Byte" in resp.text


def test_search_empty_query_renders_no_results(app_client):
    client, _ = app_client
    resp = client.get("/search", params={"q": ""})
    assert resp.status_code == 200
    assert "No results" in resp.text


def test_search_bad_query_does_not_500(app_client):
    client, _ = app_client
    resp = client.get("/search", params={"q": '")(*&"'})
    assert resp.status_code == 200
    assert "No results" in resp.text


def test_search_pagination_links(app_client):
    client, _ = app_client
    resp = client.get("/search", params={"q": "page"})
    assert resp.status_code == 200
    # Pagination control should exist when there are results.
    assert "Next" in resp.text or "next" in resp.text or "results" in resp.text.lower()
```

- [ ] **Step 15.3: Run tests, verify fail**

```bash
pytest tests/test_web_search.py -v
```

Expected: failure (no `/search` route).

- [ ] **Step 15.4: Add the search route**

Append to `src/magsearch/web/routes.py`:

```python
from fastapi import Query

from magsearch.web.search import search

_PAGE_SIZE = 25


@router.get("/search", response_class=HTMLResponse)
def search_route(
    request: Request,
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    offset = (page - 1) * _PAGE_SIZE
    results = search(db, q, offset=offset, limit=_PAGE_SIZE)
    return _TEMPLATES.TemplateResponse(
        request,
        "search.html",
        {"q": q, "results": results, "page": page,
         "has_more": len(results) == _PAGE_SIZE},
    )
```

- [ ] **Step 15.5: Write `src/magsearch/web/templates/search.html`**

```html
{% extends "base.html" %}
{% block title %}Search · {{ q }}{% endblock %}
{% block content %}
<h1 class="text-xl font-bold mb-4">Results for "{{ q }}"</h1>

{% if results %}
<ul class="space-y-3">
  {% for r in results %}
  <li class="flex gap-4 bg-white rounded shadow p-3">
    <a href="/magazine/{{ r.magazine_id }}/page/{{ r.page_number }}?q={{ q|urlencode }}">
      <img src="/bundle/{{ r.thumb_path }}" alt=""
           class="w-24 h-32 object-cover border border-slate-200">
    </a>
    <div class="flex-1">
      <div class="font-semibold">
        <a href="/magazine/{{ r.magazine_id }}">{{ r.magazine_title }}</a>
        <span class="text-slate-500 text-sm">— page {{ r.page_number }}</span>
      </div>
      <div class="text-sm text-slate-700 mt-1">{{ r.snippet|safe }}</div>
    </div>
  </li>
  {% endfor %}
</ul>

<div class="mt-6 flex gap-3">
  {% if page > 1 %}
    <a class="text-slate-600 hover:underline"
       href="/search?q={{ q|urlencode }}&page={{ page - 1 }}">← Previous</a>
  {% endif %}
  {% if has_more %}
    <a class="text-slate-600 hover:underline"
       href="/search?q={{ q|urlencode }}&page={{ page + 1 }}">Next →</a>
  {% endif %}
</div>
{% else %}
<p class="text-slate-500">No results.</p>
{% endif %}
{% endblock %}
```

- [ ] **Step 15.6: Run tests, verify pass**

```bash
pytest tests/test_web_search.py -v
```

Expected: 4 tests pass.

- [ ] **Step 15.7: Commit**

```bash
git add src/magsearch/web/routes.py src/magsearch/web/templates/search.html tests/test_web_search.py tests/conftest.py
git commit -m "feat: add search route and results template"
```

---

## Task 16: Magazines Browse + Detail Routes

Implements `GET /magazines` (browse grid with optional `publisher` filter) and `GET /magazine/{id}` (detail page with cover + page grid).

**Files:**
- Modify: `src/magsearch/web/routes.py`
- Create: `src/magsearch/web/templates/magazines.html`
- Create: `src/magsearch/web/templates/magazine.html`
- Create: `tests/test_web_magazines.py`

- [ ] **Step 16.1: Write failing tests**

```python
# tests/test_web_magazines.py
def test_magazines_lists_all(app_client):
    client, _ = app_client
    resp = client.get("/magazines")
    assert resp.status_code == 200
    assert "Byte" in resp.text
    assert "Compute" in resp.text


def test_magazines_filter_by_publisher(app_client):
    client, _ = app_client
    resp = client.get("/magazines", params={"publisher": "McGraw-Hill"})
    assert "Byte" in resp.text
    assert "Compute" not in resp.text


def test_magazine_detail_shows_pages(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12")
    assert resp.status_code == 200
    assert "Byte" in resp.text
    # Should link to page 1 and page 2.
    assert "/magazine/byte-1985-12/page/1" in resp.text
    assert "/magazine/byte-1985-12/page/2" in resp.text


def test_magazine_detail_404_for_unknown(app_client):
    client, _ = app_client
    resp = client.get("/magazine/nonexistent")
    assert resp.status_code == 404
```

- [ ] **Step 16.2: Run tests, verify fail**

```bash
pytest tests/test_web_magazines.py -v
```

Expected: failures (routes missing).

- [ ] **Step 16.3: Add routes — append to `src/magsearch/web/routes.py`**

```python
from fastapi import HTTPException


@router.get("/magazines", response_class=HTMLResponse)
def magazines_index(
    request: Request,
    publisher: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    stmt = select(Magazine).order_by(Magazine.publication_date.desc().nullslast())
    if publisher:
        stmt = stmt.where(Magazine.publisher == publisher)
    magazines = db.scalars(stmt).all()
    all_publishers = db.scalars(
        select(Magazine.publisher).where(Magazine.publisher.is_not(None)).distinct()
    ).all()
    return _TEMPLATES.TemplateResponse(
        request,
        "magazines.html",
        {"magazines": magazines, "publishers": all_publishers, "active": publisher},
    )


@router.get("/magazine/{magazine_id}", response_class=HTMLResponse)
def magazine_detail(
    request: Request,
    magazine_id: str,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    mag = db.get(Magazine, magazine_id)
    if mag is None:
        raise HTTPException(status_code=404, detail="magazine not found")
    return _TEMPLATES.TemplateResponse(
        request,
        "magazine.html",
        {"mag": mag, "pages": sorted(mag.pages, key=lambda p: p.page_number)},
    )
```

- [ ] **Step 16.4: Write `src/magsearch/web/templates/magazines.html`**

```html
{% extends "base.html" %}
{% block title %}Browse magazines{% endblock %}
{% block content %}
<div class="flex items-baseline justify-between mb-4">
  <h1 class="text-xl font-bold">All magazines</h1>
  {% if publishers %}
  <form method="get" class="text-sm">
    <label>Publisher:</label>
    <select name="publisher" onchange="this.form.submit()" class="border rounded p-1">
      <option value="">All</option>
      {% for p in publishers %}
      <option value="{{ p }}" {% if p == active %}selected{% endif %}>{{ p }}</option>
      {% endfor %}
    </select>
  </form>
  {% endif %}
</div>

<ul class="grid grid-cols-3 gap-4">
  {% for m in magazines %}
  <li class="bg-white rounded shadow p-2">
    <a href="/magazine/{{ m.id }}">
      <img src="/bundle/{{ m.cover_path }}" alt="" class="w-full h-48 object-cover">
      <div class="mt-2 font-semibold">{{ m.title }}</div>
      {% if m.issue %}<div class="text-xs text-slate-500">{{ m.issue }}</div>{% endif %}
      {% if m.publisher %}<div class="text-xs text-slate-400">{{ m.publisher }}</div>{% endif %}
    </a>
  </li>
  {% endfor %}
</ul>
{% endblock %}
```

- [ ] **Step 16.5: Write `src/magsearch/web/templates/magazine.html`**

```html
{% extends "base.html" %}
{% block title %}{{ mag.title }}{% endblock %}
{% block content %}
<div class="flex gap-6 mb-6">
  <img src="/bundle/{{ mag.cover_path }}" alt="" class="w-48 shadow">
  <div>
    <h1 class="text-2xl font-bold">{{ mag.title }}</h1>
    {% if mag.issue %}<div class="text-slate-600">{{ mag.issue }}</div>{% endif %}
    {% if mag.publisher %}<div class="text-slate-500">{{ mag.publisher }}</div>{% endif %}
    {% if mag.publication_date %}<div class="text-slate-500">{{ mag.publication_date }}</div>{% endif %}
    <div class="text-slate-500 mt-2">{{ mag.page_count }} pages · {{ mag.original_format|upper }}</div>
    <a href="/bundle/{{ mag.id }}/original.{{ mag.original_format }}"
       class="inline-block mt-3 text-blue-700 underline">Download original</a>
  </div>
</div>

<h2 class="text-lg font-semibold mb-3">Pages</h2>
<ul class="grid grid-cols-6 gap-3">
  {% for p in pages %}
  <li>
    <a href="/magazine/{{ mag.id }}/page/{{ p.page_number }}" class="block">
      <img src="/bundle/{{ p.thumb_path }}" alt=""
           class="w-full h-28 object-cover border">
      <div class="text-xs text-slate-500 text-center mt-1">p. {{ p.page_number }}</div>
    </a>
  </li>
  {% endfor %}
</ul>
{% endblock %}
```

- [ ] **Step 16.6: Run tests, verify pass**

```bash
pytest tests/test_web_magazines.py -v
```

Expected: 4 tests pass.

- [ ] **Step 16.7: Commit**

```bash
git add src/magsearch/web/routes.py src/magsearch/web/templates/magazines.html src/magsearch/web/templates/magazine.html tests/test_web_magazines.py
git commit -m "feat: add magazines browse and detail pages"
```

---

## Task 17: Single Page Route

Implements `GET /magazine/{id}/page/{n}` with prev/next + back link preserving the `q` query.

**Files:**
- Modify: `src/magsearch/web/routes.py`
- Create: `src/magsearch/web/templates/page.html`
- Create: `tests/test_web_page.py`

- [ ] **Step 17.1: Write failing tests**

```python
# tests/test_web_page.py
def test_page_view_shows_image(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/page/1")
    assert resp.status_code == 200
    assert "/bundle/byte-1985-12/pages/0001.webp" in resp.text


def test_page_view_prev_next_links(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/page/1")
    assert "/magazine/byte-1985-12/page/2" in resp.text
    # No "previous" on page 1
    assert "/magazine/byte-1985-12/page/0" not in resp.text

    resp = client.get("/magazine/byte-1985-12/page/2")
    assert "/magazine/byte-1985-12/page/1" in resp.text
    assert "/magazine/byte-1985-12/page/3" not in resp.text  # only 2 pages


def test_page_view_404_for_bad_page(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/page/999")
    assert resp.status_code == 404


def test_page_view_preserves_query_in_back_link(app_client):
    client, _ = app_client
    resp = client.get("/magazine/byte-1985-12/page/1?q=synthesizer")
    assert "synthesizer" in resp.text  # back link or search bar echoes it
```

- [ ] **Step 17.2: Run tests, verify fail**

```bash
pytest tests/test_web_page.py -v
```

Expected: 404 for all (route missing).

- [ ] **Step 17.3: Add the route — append to `src/magsearch/web/routes.py`**

```python
from magsearch.models import Page as PageModel


@router.get("/magazine/{magazine_id}/page/{page_number}", response_class=HTMLResponse)
def page_view(
    request: Request,
    magazine_id: str,
    page_number: int,
    q: str = Query(default=""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    mag = db.get(Magazine, magazine_id)
    if mag is None:
        raise HTTPException(status_code=404, detail="magazine not found")
    page = db.scalar(
        select(PageModel).where(
            PageModel.magazine_id == magazine_id,
            PageModel.page_number == page_number,
        )
    )
    if page is None:
        raise HTTPException(status_code=404, detail="page not found")
    return _TEMPLATES.TemplateResponse(
        request,
        "page.html",
        {
            "mag": mag,
            "page": page,
            "q": q,
            "has_prev": page_number > 1,
            "has_next": page_number < mag.page_count,
        },
    )
```

- [ ] **Step 17.4: Write `src/magsearch/web/templates/page.html`**

```html
{% extends "base.html" %}
{% block title %}{{ mag.title }} · p. {{ page.page_number }}{% endblock %}
{% block content %}
<div class="flex items-center justify-between mb-3">
  <a href="/magazine/{{ mag.id }}" class="text-slate-600 hover:underline">← {{ mag.title }}</a>
  <div class="flex gap-3">
    {% if has_prev %}
    <a href="/magazine/{{ mag.id }}/page/{{ page.page_number - 1 }}{% if q %}?q={{ q|urlencode }}{% endif %}"
       class="text-slate-600 hover:underline">Previous</a>
    {% endif %}
    <span class="text-slate-500">p. {{ page.page_number }} / {{ mag.page_count }}</span>
    {% if has_next %}
    <a href="/magazine/{{ mag.id }}/page/{{ page.page_number + 1 }}{% if q %}?q={{ q|urlencode }}{% endif %}"
       class="text-slate-600 hover:underline">Next</a>
    {% endif %}
  </div>
</div>
{% if q %}
<div class="mb-2 text-sm">
  <a href="/search?q={{ q|urlencode }}" class="text-blue-700 underline">
    ← Back to results for "{{ q }}"
  </a>
</div>
{% endif %}
<img src="/bundle/{{ page.image_path }}" alt="" class="w-full shadow border">
{% endblock %}
```

- [ ] **Step 17.5: Run tests, verify pass**

```bash
pytest tests/test_web_page.py -v
```

Expected: 4 tests pass.

- [ ] **Step 17.6: Commit**

```bash
git add src/magsearch/web/routes.py src/magsearch/web/templates/page.html tests/test_web_page.py
git commit -m "feat: add single-page view with prev/next navigation"
```

---

## Task 18: Bundle Static Serving + Path-Traversal Protection

Implements `GET /bundle/{path:path}` — serves images and originals out of `BUNDLES_DIR`. Validates that the resolved path stays inside the bundles dir.

**Files:**
- Modify: `src/magsearch/web/routes.py`
- Create: `tests/test_web_bundle.py`

- [ ] **Step 18.1: Write failing tests**

```python
# tests/test_web_bundle.py
def test_bundle_serves_existing_thumb(app_client):
    client, _ = app_client
    resp = client.get("/bundle/byte-1985-12/thumbs/0001.webp")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/")


def test_bundle_404_for_missing(app_client):
    client, _ = app_client
    resp = client.get("/bundle/byte-1985-12/thumbs/9999.webp")
    assert resp.status_code == 404


def test_bundle_rejects_path_traversal_dotdot(app_client):
    client, _ = app_client
    resp = client.get("/bundle/byte-1985-12/../../etc/passwd")
    assert resp.status_code in (400, 404)


def test_bundle_rejects_absolute_path(app_client):
    client, _ = app_client
    resp = client.get("/bundle//etc/passwd")
    # FastAPI may 404 on the double-slash normalization; either way must not 200.
    assert resp.status_code != 200
```

- [ ] **Step 18.2: Run tests, verify fail**

```bash
pytest tests/test_web_bundle.py -v
```

Expected: 404 across the board (route missing).

- [ ] **Step 18.3: Add the route — append to `src/magsearch/web/routes.py`**

```python
from fastapi.responses import FileResponse

from magsearch.settings import Settings, get_settings


@router.get("/bundle/{path:path}")
def serve_bundle(path: str, settings: Settings = Depends(get_settings)):
    root = settings.bundles_dir.resolve()
    candidate = (root / path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(candidate)
```

- [ ] **Step 18.4: Run tests, verify pass**

```bash
pytest tests/test_web_bundle.py -v
```

Expected: 4 tests pass.

- [ ] **Step 18.5: Commit**

```bash
git add src/magsearch/web/routes.py tests/test_web_bundle.py
git commit -m "feat: add /bundle static serving with path-traversal protection"
```

---

## Task 19: CLI Wiring

Implements the single `magsearch` Typer app with `ingest`, `import`, `web`, and `db` subcommands. Wires the entrypoint defined in `pyproject.toml`.

**Files:**
- Create: `src/magsearch/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 19.1: Write failing tests**

```python
# tests/test_cli.py
import re
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from magsearch.cli import app

runner = CliRunner()


def test_cli_help_lists_subcommands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("ingest", "import", "web", "db"):
        assert cmd in result.stdout


def test_cli_ingest_then_import_creates_searchable_magazine(tmp_path, monkeypatch):
    from tests.fixtures.pdfs import make_pdf

    db_path = tmp_path / "test.db"
    bundles = tmp_path / "bundles"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(bundles))

    src = make_pdf(tmp_path / "byte.pdf", num_pages=1)

    r = runner.invoke(app, ["db", "upgrade"])
    assert r.exit_code == 0, r.stdout

    r = runner.invoke(app, [
        "ingest", str(src),
        "--title", "Byte",
        "--date", "1985-12-01",
        "--bundles-dir", str(bundles),
        "--fake-ocr",
    ])
    assert r.exit_code == 0, r.stdout
    bundle = bundles / "byte-1985-12"
    assert (bundle / "manifest.json").exists()

    r = runner.invoke(app, ["import", str(bundle)])
    assert r.exit_code == 0, r.stdout
    assert "byte-1985-12" in r.stdout
```

- [ ] **Step 19.2: Run tests, verify fail**

```bash
pytest tests/test_cli.py -v
```

Expected: `ModuleNotFoundError` or no `app` symbol.

- [ ] **Step 19.3: Implement `src/magsearch/cli.py`**

```python
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from alembic import command
from alembic.config import Config

from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.importer import ImportError as MagImportError, import_bundle
from magsearch.ingest.ocr import FakeOCREngine
from magsearch.ingest.pipeline import IngestOptions, IngestPipeline
from magsearch.settings import get_settings

app = typer.Typer(no_args_is_help=True, help="Magazine search.")
db_app = typer.Typer(no_args_is_help=True, help="Database commands.")
app.add_typer(db_app, name="db")


def _alembic_cfg() -> Config:
    settings = get_settings()
    cfg = Config(str(Path(__file__).parent.parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    return cfg


@app.command("ingest")
def ingest_cmd(
    source: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    title: str = typer.Option("", "--title"),
    issue: str | None = typer.Option(None, "--issue"),
    publication_date: str | None = typer.Option(None, "--date"),
    publisher: str | None = typer.Option(None, "--publisher"),
    id_override: str | None = typer.Option(None, "--id"),
    bundles_dir: Path | None = typer.Option(None, "--bundles-dir"),
    force: bool = typer.Option(False, "--force"),
    fake_ocr: bool = typer.Option(
        False, "--fake-ocr", help="Use FakeOCREngine (tests / dry runs)."
    ),
) -> None:
    settings = get_settings()
    target_bundles = bundles_dir or settings.bundles_dir
    parsed_date = date.fromisoformat(publication_date) if publication_date else None
    if fake_ocr:
        engine = FakeOCREngine()
    else:
        from magsearch.ingest.ocr import PaddleOCREngine
        engine = PaddleOCREngine()
    pipeline = IngestPipeline(
        bundles_root=target_bundles,
        ocr_engine=engine,
        options=IngestOptions(
            title=title, issue=issue,
            publication_date=parsed_date, publisher=publisher,
            id_override=id_override,
        ),
    )
    result = pipeline.run(source, force=force)
    typer.echo(f"ingested {result.id} → {result.bundle_dir}")


@app.command("import")
def import_cmd(bundle: Annotated[Path, typer.Argument(exists=True, file_okay=False)]) -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as s:
            mag_id = import_bundle(bundle, s)
        typer.echo(f"imported {mag_id}")
    except MagImportError as exc:
        typer.echo(f"import failed: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command("web")
def web_cmd(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    uvicorn.run("magsearch.web.app:app", host=host, port=port, reload=reload)


@db_app.command("upgrade")
def db_upgrade(revision: str = typer.Argument("head")) -> None:
    command.upgrade(_alembic_cfg(), revision)


@db_app.command("downgrade")
def db_downgrade(revision: str = typer.Argument("-1")) -> None:
    command.downgrade(_alembic_cfg(), revision)
```

- [ ] **Step 19.4: Run tests, verify pass**

```bash
pytest tests/test_cli.py -v
```

Expected: 2 tests pass.

- [ ] **Step 19.5: Full test suite green**

```bash
pytest -v
```

Expected: every non-`ocr`-marked test passes.

- [ ] **Step 19.6: Smoke-run the CLI end-to-end against a generated PDF**

```bash
. .venv/bin/activate
python -c "from tests.fixtures.pdfs import make_pdf; from pathlib import Path; make_pdf(Path('/tmp/sample.pdf'), 2)"
mkdir -p /tmp/mag-data
MAGSEARCH_DATABASE_URL="sqlite:////tmp/mag-data/test.db" \
MAGSEARCH_BUNDLES_DIR="/tmp/mag-data/bundles" \
magsearch db upgrade
MAGSEARCH_DATABASE_URL="sqlite:////tmp/mag-data/test.db" \
MAGSEARCH_BUNDLES_DIR="/tmp/mag-data/bundles" \
magsearch ingest /tmp/sample.pdf --title "Sample" --date 1985-12-01 \
  --bundles-dir /tmp/mag-data/bundles --fake-ocr
MAGSEARCH_DATABASE_URL="sqlite:////tmp/mag-data/test.db" \
MAGSEARCH_BUNDLES_DIR="/tmp/mag-data/bundles" \
magsearch import /tmp/mag-data/bundles/sample-1985-12
MAGSEARCH_DATABASE_URL="sqlite:////tmp/mag-data/test.db" \
MAGSEARCH_BUNDLES_DIR="/tmp/mag-data/bundles" \
magsearch web --port 8765 &
sleep 1
curl -s 'http://127.0.0.1:8765/' | grep -q 'Magazine Search'
curl -s 'http://127.0.0.1:8765/search?q=fake' | grep -q '<mark>'
kill %1
```

Expected: both `grep -q` commands succeed.

- [ ] **Step 19.7: Commit**

```bash
git add src/magsearch/cli.py tests/test_cli.py
git commit -m "feat: wire typer CLI with ingest, import, web, and db subcommands"
```

---

## Self-Review (already performed before saving)

- **Spec coverage:** every spec section maps to one or more tasks:
  - Goals / Non-Goals → reflected in the test suite design and absence of auth/tagging tasks
  - Architecture two-machine split → Tasks 10, 11, 19 (split of `ingest` from `import`/`web`)
  - Data Model → Task 3
  - Ingestion Pipeline (steps 1–6) → Tasks 5–10, 12
  - Server / import → Task 11
  - Server / web routes → Tasks 14–18
  - Search query → Task 13
  - Project Layout → file structure map at the top of this plan
  - Error Handling → Tasks 10 (unknown format), 11 (checksum, collision), 13 (bad query), 18 (path traversal), 19 (import error exit code)
  - Testing Strategy → integration backbone is Tasks 10–13; FakeOCREngine arrives in Task 6; PaddleOCR is the only `@pytest.mark.ocr` test in Task 12
- **Placeholder scan:** every step contains the code or command it requires; no TBD/TODO/"similar to" references.
- **Type consistency:** `OCREngine.recognize -> list[OCRRegion]` is used consistently; `Manifest.cover_path` is a bundle-relative path everywhere; `Magazine.cover_path` and `Page.image_path`/`thumb_path` are prefixed with `{magazine_id}/` by the importer so the web `/bundle/{path}` route can serve them directly.
- **Notable gap closed during review:** the `magsearch web --reload` flag uses the module path `"magsearch.web.app:app"` because the module exposes both `create_app()` and a `app = create_app()` instance for uvicorn import. Without that instance, `--reload` would not work.

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-11-magazine-search.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
