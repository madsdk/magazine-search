# Magazine Search — Design

**Date:** 2026-05-11
**Status:** Approved, ready for implementation planning

A self-hosted web app for searching the full text of old magazine scans. Magazines are ingested from PDF, CBR, or CBZ files; pages are rendered to images and OCR'd; the resulting text is indexed for full-text search. Each search hit identifies a magazine, a page number, and a snippet, and links to a view of that page.

## Goals

- Search across a personal archive of old magazines (image-based scans) by typing a term and getting back the magazine + page where it appears.
- Run on modest hardware (small VPS, no GPU) for the user-facing app.
- Run OCR on a separate workstation with a GPU, then ship results to the server.
- Stay small: one Python package, SQLite database, no external services.

## Non-Goals

- Multi-user accounts, permissions, or audit trails. No auth at all.
- In-browser PDF rendering, page flipping, or comic-reader UX.
- Tagging, collections, bookmarks, or notes. (Schema leaves room for these later.)
- Word-level hit highlighting on the page image. (PaddleOCR's bounding boxes are written to disk so this can be added later without re-OCR.)
- Versioned OCR runs. Re-ingest replaces; no history.
- Real-time / watch-folder ingestion. Operator runs the CLI.

## Decisions Made During Brainstorming

| # | Decision | Rationale |
|---|---|---|
| 1 | Personal-but-accessible deployment (home server / small VPS) | Matches the user's intent and rules out multi-user features |
| 2 | PaddleOCR as OCR engine | Substantially better accuracy on noisy scans than Tesseract; pluggable interface allows swapping later |
| 3 | English only | Single language pack; simplifies tokenizer setup |
| 4 | Search results: list with thumbnail + snippet; click opens the page | Polished without requiring word-box rendering |
| 5 | No auth | "Personal but accessible" interpreted as "behind a VPN / private URL"; auth can be layered later as a FastAPI dependency |
| 6 | Medium collection (hundreds of magazines, tens of thousands of pages) | SQLite + FTS5 is the right tool at this scale |
| 7 | Page images are the canonical display artifact | Unifies PDF/CBR/CBZ; no in-browser PDF rendering needed |
| 8 | CLI-driven, file-based hand-off between GPU box and server | Simple, debuggable, no network coupling between machines |
| 9 | Standard metadata + auto-extracted cover image | Enough for a browsable library; no premature tagging system |
| 10 | Stable IDs; idempotent re-ingest replaces | Safe to re-run; no version history needed |

## Architecture

Two physical machines, one repository, one schema. The split is by hardware, not by service boundary. The two sides communicate exclusively through a directory of files (the "bundle").

```
┌────────────────── GPU box (workstation) ─────────────────────────┐
│                                                                  │
│   magsearch ingest <file>     CLI on the GPU box                 │
│     ├─ reads:  PDF / CBR / CBZ                                   │
│     ├─ uses:   PaddleOCR (GPU)                                   │
│     └─ writes: ./bundles/<magazine-id>/                          │
│                  ├─ original.<ext>                               │
│                  ├─ pages/0001.webp, 0002.webp, ...              │
│                  ├─ thumbs/0001.webp, ...                        │
│                  ├─ cover.webp                                   │
│                  ├─ ocr/0001.json, ...   (raw OCR output)        │
│                  └─ manifest.json                                │
│                                                                  │
└──────────────────────────────┬───────────────────────────────────┘
                               │  rsync -a bundles/<id>/  server:/data/bundles/
                               ▼
┌──────────────────────── Server (small VPS) ──────────────────────┐
│                                                                  │
│   magsearch import <bundle>          magsearch web               │
│     reads bundle dir,                  FastAPI + Jinja2           │
│     verifies checksums,                serves search UI,          │
│     populates SQLite + FTS5            page images, originals     │
│                                                                  │
│   /data/                                                         │
│     ├─ magsearch.db   (SQLite, owned by server)                  │
│     └─ bundles/<id>/...                                          │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

Key properties:

- **No runtime network calls between machines.** The contract is `manifest.json` plus the file layout under a bundle directory.
- **One Python package, three entrypoints** (`magsearch ingest`, `magsearch import`, `magsearch web`) via a single Typer CLI.
- **SQLAlchemy + Alembic live in the shared package**, but only the server box runs them against the DB. The GPU box never touches `magsearch.db`.
- **PaddleOCR is an optional extra** (`pip install -e ".[ocr]"`). The server installs the base package; only the GPU box installs the OCR extra.

## Data Model

SQLite, managed by Alembic. Three tables/objects:

### `magazines`

| column | type | notes |
|---|---|---|
| `id` | TEXT (PK) | stable slug, e.g. `byte-1985-12`; falls back to first 12 chars of `content_hash` if metadata is missing. Operator may override with `--id`. |
| `title` | TEXT NOT NULL | e.g. "Byte" |
| `issue` | TEXT NULL | e.g. "Vol 10 No 12" |
| `publication_date` | DATE NULL | for sorting / filtering |
| `publisher` | TEXT NULL | |
| `original_filename` | TEXT NOT NULL | as found at ingest |
| `original_format` | TEXT NOT NULL | `pdf` \| `cbr` \| `cbz` |
| `page_count` | INTEGER NOT NULL | |
| `content_hash` | TEXT NOT NULL | SHA-256 of the original file; drives idempotent re-ingest and collision detection |
| `cover_path` | TEXT NOT NULL | relative path to `cover.webp` under the bundles dir |
| `ocr_engine` | TEXT NOT NULL | `paddleocr` initially |
| `ocr_engine_version` | TEXT NOT NULL | e.g. `2.7.0` — lets the operator spot stale OCR later |
| `ingested_at` | TIMESTAMP NOT NULL | |

### `pages`

| column | type | notes |
|---|---|---|
| `id` | INTEGER (PK) | rowid; referenced by `pages_fts.content_rowid` |
| `magazine_id` | TEXT NOT NULL (FK → magazines.id ON DELETE CASCADE) | |
| `page_number` | INTEGER NOT NULL | 1-based |
| `image_path` | TEXT NOT NULL | relative path to `pages/NNNN.webp` |
| `thumb_path` | TEXT NOT NULL | relative path to `thumbs/NNNN.webp` |
| `text` | TEXT NOT NULL | concatenated OCR text for the page (FTS indexes this) |

Unique constraint on `(magazine_id, page_number)`.

### `pages_fts` (FTS5 virtual table)

```sql
CREATE VIRTUAL TABLE pages_fts USING fts5(
    text,
    content='pages',
    content_rowid='id',
    tokenize = 'porter unicode61 remove_diacritics 2'
);
```

Plus the three external-content sync triggers (`pages_ai`, `pages_ad`, `pages_au`). Porter stemming gives "synthesizer / synthesizers / synthesized" interchangeably, which matters for usability.

### Things deliberately not in the DB

- **Per-word bounding boxes.** Not needed for the chosen search-result UX. Raw PaddleOCR output (which includes them) is preserved in `ocr/NNNN.json` on disk so a future `word_boxes` column can be backfilled without re-OCR.
- **Page images.** Stored on disk under `/data/bundles/<id>/`; the DB only stores relative paths.

## Ingestion Pipeline (GPU box)

`magsearch ingest <file> [--title ...] [--issue ...] [--date ...] [--publisher ...] [--id ...] [--force] [--workers N]`

Six deterministic steps:

1. **`detect_format`** — identify PDF / CBR / CBZ by magic bytes (not file extension).
2. **`extract_pages`** — iterator of `(page_num, PIL.Image)`:
   - PDF: render each page via PyMuPDF (`fitz`).
   - CBZ: unzip, read images in lexical name order.
   - CBR: extract via `unar` or `unrar` subprocess (required on `PATH`).
3. **`normalize_pages`** — encode each page as WebP at ~1800px long edge, quality 80. Generate thumbnails at 400px long edge, quality 70. Cover image is a copy of the first thumbnail.
4. **`ocr_pages`** — `PaddleOCR(lang='en')` over the page images, batched. For each page:
   - Write raw output (per-region `{text, bbox, confidence}`) to `ocr/NNNN.json`.
   - Concatenate text in reading order (sort regions by y-then-x of their bbox) for indexing.
5. **`write_manifest`** — `manifest.json` containing: `id`, `title`, `issue`, `publication_date`, `publisher`, `page_count`, `content_hash`, `original_filename`, `original_format`, `ocr_engine`, `ocr_engine_version`, per-page file paths, and `schema_version` (the manifest format version — currently 1).
6. **`checksum`** — SHA-256 of every output file recorded in the manifest. Used by the importer to verify bundle integrity.

**Stable ID generation:** if `--id` is given, use it. Otherwise, if `title` and `publication_date` are present, slug-ify `title-YYYY-MM`. Otherwise, use the first 12 hex chars of `content_hash`.

**Idempotency:** if `bundles/<id>/manifest.json` already exists with a matching `content_hash`, the pipeline exits with "already ingested" unless `--force` is passed. Each step writes outputs to a temp path and renames on completion, so a crashed run can be resumed: re-running skips steps whose outputs already exist.

**Resource control:** `--workers N` controls PDF rendering / image-encoding parallelism (default: CPU count). PaddleOCR is single-process — it manages its own GPU batching.

## Server Side

### `magsearch import <bundle-dir>`

Idempotent DB import. All steps in a single transaction:

1. Parse and validate `manifest.json` (pydantic).
2. Verify every file's SHA-256 against the manifest.
3. `INSERT OR REPLACE INTO magazines` keyed by `id`.
4. `DELETE FROM pages WHERE magazine_id = ?` (the FTS sync triggers cascade), then bulk insert new pages from the manifest.
5. Commit. Either the magazine is fully re-indexed or the DB is unchanged.

**Slug collision detection:** if a different `content_hash` already exists in the DB for the same `id`, the importer refuses, prints both hashes and bundle paths, and suggests `--id` override.

### `magsearch web` — FastAPI app

Server-rendered HTML via Jinja2. Tailwind via CDN for styling. No SPA, no client-side framework.

| route | renders |
|---|---|
| `GET /` | landing: search box, recently ingested magazines, a few covers |
| `GET /search?q=<q>&page=<n>` | result list: thumbnail, magazine title, page number, snippet with `<mark>`-wrapped hits, pagination |
| `GET /magazines` | grid of covers, sorted by `publication_date` desc; filter by `publisher` |
| `GET /magazine/{id}` | magazine detail: cover, full metadata, page grid (thumbnails), link to download original |
| `GET /magazine/{id}/page/{n}` | single-page view: full-resolution page image, prev/next links, back-to-results link preserving query |
| `GET /bundle/{id}/{path...}` | static file serve from `/data/bundles/{id}/`; path is validated to stay under the bundle directory |

**Search query** (single SQL statement):

```sql
SELECT
    magazines.id, magazines.title,
    pages.page_number, pages.thumb_path,
    snippet(pages_fts, 0, '<mark>', '</mark>', '…', 16) AS snippet
FROM pages_fts
JOIN pages     ON pages_fts.rowid = pages.id
JOIN magazines ON pages.magazine_id = magazines.id
WHERE pages_fts MATCH ?
ORDER BY rank
LIMIT 25 OFFSET ?
```

Queries go through a small sanitizer: balance double quotes, escape FTS5 special characters outside of quoted phrases, and reject pathological inputs (empty, only-punctuation). Supports `"phrase queries"` natively because FTS5 does.

## Project Layout

```
magsearch/
├── pyproject.toml              # base deps; [project.optional-dependencies] ocr = ["paddleocr", ...]
├── alembic.ini
├── alembic/versions/...
├── src/magsearch/
│   ├── __init__.py
│   ├── cli.py                  # typer app: ingest, import, web, db
│   ├── db.py                   # SQLAlchemy engine + session factory
│   ├── models.py               # Magazine, Page
│   ├── manifest.py             # pydantic models — the cross-machine contract
│   ├── ingest/
│   │   ├── __init__.py
│   │   ├── pipeline.py         # the 6-step orchestrator
│   │   ├── formats.py          # PDF / CBZ / CBR readers → iterator of (page_num, PIL.Image)
│   │   ├── normalize.py        # resize / encode WebP, thumbnails, cover
│   │   ├── ocr.py              # OCREngine Protocol + PaddleOCREngine impl
│   │   └── ids.py              # slug + content hash
│   ├── importer.py             # bundle dir → DB
│   └── web/
│       ├── app.py              # FastAPI app factory
│       ├── routes.py
│       ├── search.py           # FTS query construction + sanitization
│       └── templates/...
└── tests/
    ├── fixtures/               # tiny sample PDFs/CBZs with known text
    ├── test_ingest_*.py
    ├── test_importer.py
    ├── test_search.py
    └── test_web.py
```

`OCREngine` is a `typing.Protocol`:

```python
class OCREngine(Protocol):
    name: str
    version: str
    def recognize(self, image: PIL.Image.Image) -> list[OCRRegion]: ...
```

Tests use `FakeOCREngine` that returns deterministic strings keyed by image content hash, so the suite does not depend on PaddleOCR or a GPU.

## Error Handling

- **OCR failure on a single page:** record the page with empty text and a warning; ingest continues. The page is still browseable.
- **Corrupt original file:** ingest aborts before any output is written; bundles dir and DB are untouched.
- **Missing `unrar`/`unar` for CBR:** ingest aborts with an actionable message.
- **Partial bundle on import (checksum mismatch):** import aborts; DB rolled back.
- **Slug collision** (different `content_hash` for an existing `id`): import refuses; prints both hashes; suggests `--id` override.
- **Bad search query** (unbalanced quotes, FTS5 syntax error): caught in the sanitizer; web returns "no results" with the raw query echoed back, not a 500.
- **Logging:** stdlib `logging`, INFO to stdout by default, DEBUG with `--verbose`. No structured logging framework — this is a personal app.

## Testing Strategy

- **Unit:** format readers (PDF/CBR/CBZ → page images), id generation, manifest serialization/validation, FTS query sanitizer, snippet rendering.
- **Integration with `FakeOCREngine`:** end-to-end ingest of fixture files → bundle on disk → import → DB rows + FTS hits. This is the backbone of the test suite. Runs in CI; no GPU, no PaddleOCR install.
- **Smoke test with real PaddleOCR:** one test marked `@pytest.mark.ocr`, skipped by default, runs locally on the GPU box against a known-good fixture. Verifies the real engine still produces expected text.
- **Web tests:** FastAPI `TestClient` against a populated test DB; assert routes, search results, file serving, and path-traversal protection on `/bundle/`.
- **No browser/E2E tests.** UI is server-rendered HTML; complexity doesn't warrant it.

## Open Items for the Implementation Plan

These are minor and deferred to the planning stage, not part of the design:

- Concrete Alembic migration sequence and exact trigger SQL for FTS sync.
- HTML template structure for the Jinja2 views.
- CLI flag names for `magsearch db init` / `magsearch db migrate` convenience commands.
- Choice of WSGI/ASGI server for deployment (likely `uvicorn` behind nginx).
