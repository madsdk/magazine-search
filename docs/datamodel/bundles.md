# Bundle format

A **bundle** is a self-contained, on-disk representation of a single magazine
issue. The bundle is the unit that the importer (`magsearch.importer.import_bundle`)
consumes to populate the database, and the unit that the web app serves files
from at request time.

This document describes the on-disk layout and the `manifest.json` schema in
enough detail to build an independent tool that produces bundles `magsearch`
will accept.

The canonical producer is `magsearch.ingest.pipeline.IngestPipeline`. Read it
alongside this doc when a fine detail is ambiguous — the pipeline is the
reference implementation. The canonical consumer is
`magsearch.importer.import_bundle`. The manifest schema is defined as a
Pydantic model in `magsearch/manifest.py` and is validated with
`extra="forbid"` — unknown fields are rejected.

## Directory layout

A bundle is a directory. Its name is the magazine `id` (see [Bundle id](#bundle-id)).
The directory must live as an immediate child of the deployment's
`bundles_root` (configured via the app settings, served under the `/bundle/`
URL prefix).

```
<bundles_root>/
└── byte-1985-12/                 ← directory name == manifest.id
    ├── manifest.json             ← required, schema below
    ├── original.pdf              ← required, name is `original.<original_format>`
    ├── cover.webp                ← optional (omit when no pages); referenced by manifest.cover_path
    ├── pages/
    │   ├── 0001.webp             ← full-resolution page image
    │   ├── 0002.webp
    │   └── …
    ├── thumbs/
    │   ├── 0001.webp             ← thumbnail used in lists/grids
    │   ├── 0002.webp
    │   └── …
    └── ocr/
        ├── 0001.json             ← raw OCR regions (kept for reproducibility)
        ├── 0002.json
        └── …
```

The importer treats every path in `manifest.pages[*].image_path`,
`thumb_path`, `ocr_path`, `cover_path`, and `checksums[*].path` as **relative
to the bundle directory**. Use forward slashes. Do not use absolute paths or
`..` segments.

## manifest.json

UTF-8 JSON, top-level object. All fields below are required unless marked
optional. Unknown fields are rejected (`extra="forbid"` in the Pydantic
model).

### Top-level fields

| Field                | Type                              | Notes                                                                                                                              |
| -------------------- | --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `schema_version`     | integer, must be `1`              | Pinned literal. Bump only with a coordinated importer change.                                                                      |
| `id`                 | string                            | Bundle id. Must equal the directory name. See [Bundle id](#bundle-id).                                                             |
| `title`              | string                            | Magazine title, free-form. Shown in the UI.                                                                                        |
| `issue`              | string or `null` (optional)       | Issue label, free-form (e.g. `"Vol 10 No 12"`). Optional.                                                                          |
| `publication_date`   | ISO 8601 date string or `null`    | E.g. `"1985-12-01"`. Optional but used in id generation when present.                                                              |
| `publisher`          | string or `null` (optional)       | Free-form. Optional.                                                                                                               |
| `original_filename`  | string                            | The filename of the source artifact, for display/provenance only. Not used as a path.                                              |
| `original_format`    | one of `"pdf"`, `"cbz"`, `"cbr"`  | Drives the URL the UI uses to link to the original (`/bundle/<id>/original.<original_format>`).                                    |
| `page_count`         | integer ≥ 0                       | Must equal `len(pages)`. The importer does not currently re-check this, but bundle producers and downstream code may rely on it.   |
| `content_hash`       | string (lower-hex SHA-256)        | Stable fingerprint of the logical input. Used for dedupe. See [content_hash semantics](#content_hash-semantics).                   |
| `ocr_engine`         | string                            | Free-form engine name (e.g. `"paddleocr"`, `"tesseract"`, `"fake"`). Stored on the DB row.                                          |
| `ocr_engine_version` | string                            | Free-form version string. Stored on the DB row.                                                                                    |
| `cover_path`         | string                            | Bundle-relative path to the cover image, or `""` (empty string) when there is no cover. The reference pipeline uses `"cover.webp"`. |
| `pages`              | array of [PageEntry](#pageentry)  | One entry per page, in any order; `page_number` provides ordering. Must not be empty if `page_count > 0`.                          |
| `checksums`          | array of [FileChecksum](#filechecksum) | SHA-256 of every file in the bundle (except `manifest.json` itself). See [Checksums](#checksums).                              |

### PageEntry

| Field         | Type    | Notes                                                                                                       |
| ------------- | ------- | ----------------------------------------------------------------------------------------------------------- |
| `page_number` | integer | 1-based page number. Unique within the bundle (enforced by a DB unique constraint on `(magazine_id, page_number)`). |
| `image_path`  | string  | Bundle-relative path to the full-resolution page image. Reference pipeline uses `pages/{NNNN}.webp`.        |
| `thumb_path`  | string  | Bundle-relative path to the thumbnail. Reference pipeline uses `thumbs/{NNNN}.webp`.                        |
| `ocr_path`    | string  | Bundle-relative path to a JSON file containing the raw OCR output. Required in the manifest and required to exist on disk (it is checksum-verified), but the web app does not read it back from the manifest after import. |
| `text`        | string  | Plain searchable text for the page, in natural reading order. **Do not pre-escape** — the importer HTML-escapes this when writing it to the DB. May be the empty string when OCR produced nothing. |

### FileChecksum

| Field    | Type   | Notes                                                                                |
| -------- | ------ | ------------------------------------------------------------------------------------ |
| `path`   | string | Bundle-relative path. Forward slashes.                                               |
| `sha256` | string | Lower-hex SHA-256 of the file contents.                                              |

## Bundle id

The id is the bundle's primary key. It is the directory name, the value of
`manifest.id`, and the prefix of every URL the web app uses to serve files
from this bundle (`/bundle/<id>/…`).

Constraints:

- Lowercase ASCII letters, digits, and dashes only. The reference slugifier
  (`magsearch.ingest.ids.slugify`) NFKD-normalizes, lowercases, drops
  non-`[a-z0-9]` to `-`, and trims leading/trailing dashes.
- Must equal the directory name, byte for byte.
- Must be unique within a deployment.

Recommended forms (in order of preference):

1. `{slug(title)}-{YYYY}-{MM}` — for example, `byte-1985-12`. Use this when
   both a title and a publication date are available.
2. `{slug(title)}-{YYYY}-{MM}-{slug(issue)}` — when there is more than one
   issue with the same year/month.
3. `{slug(title)}-{YYYY}-{MM}-{content_hash[:6]}` (or `[:8]`, `[:12]`, `[:16]`)
   — as a last-resort disambiguator when title and date collide.
4. `content_hash[:12]` — when no title/date is available.

The reference pipeline uses these patterns in order via
`magsearch.ingest.ids.resolve_unique_id`. A producer tool is free to use any
id that meets the constraints above, but should pick a strategy that yields
the **same id for the same logical issue** across re-runs, so re-imports stay
idempotent (see [Re-import behavior](#re-import-behavior)).

## content_hash semantics

`content_hash` is the dedupe key. It is **the lower-hex SHA-256 of the
original source artifact** (the PDF/CBZ/CBR file the bundle was produced
from), computed before any normalization.

The reference pipeline computes this with
`magsearch.ingest.ids.content_hash`, which is a streaming SHA-256 of the
source file. A producer that does not have a single "source file" should
choose a stable substitute: hash a deterministically serialized representation
of the canonical inputs, and document the choice. The only hard requirement is
**stability**: the same logical magazine must yield the same `content_hash`
across regenerations.

The importer uses `content_hash` together with `id` to decide what to do:

- New `id` → insert.
- Existing `id`, **same** `content_hash` → no-op (return the existing row;
  `ingested_at` and `Page` rows are preserved).
- Existing `id`, **different** `content_hash` → raises `ImportError("id
  collision …")`. Resolution: pick a different `id` (e.g. add an issue suffix
  or a `content_hash` prefix) and re-bundle.

## Checksums

Every file under the bundle directory (recursively) **except `manifest.json`
itself** must appear in `manifest.checksums`. The importer iterates over the
list and rejects the bundle (with `ImportError("bundle missing file …")` or
`ImportError("checksum mismatch on …")`) if any file is absent or differs.

The reference pipeline collects checksums via a recursive walk of the bundle
and excludes only `manifest.json` and the transient `manifest.json.tmp`. A
producer tool should mirror this: walk the bundle after all files are
written, compute SHA-256 for each, and write them in sorted order for
determinism.

Files that are referenced from elsewhere in the manifest (page images,
thumbnails, OCR JSON, cover, `original.<fmt>`) are not implicitly checksummed
— they must appear in `checksums` explicitly.

## Image and OCR file conventions

The importer does not validate image formats, dimensions, or content. The
following conventions are produced by the reference pipeline and are what the
web layer expects browsers to render:

- **Page images** (`pages/{NNNN}.webp`): WebP, long edge ≤ 1800 px, quality 80,
  RGB. Filenames are zero-padded to 4 digits, matching `page_number`.
- **Thumbnails** (`thumbs/{NNNN}.webp`): WebP, long edge ≤ 400 px, quality 70,
  RGB.
- **Cover** (`cover.webp`): a copy of the first thumbnail. Omit the file and
  set `cover_path` to `""` if the bundle has no pages.
- **OCR JSON** (`ocr/{NNNN}.json`): an array of objects, one per detected
  region:

  ```json
  [
    {"text": "Hello", "bbox": [12.0, 34.0, 120.0, 56.0], "confidence": 0.99},
    …
  ]
  ```

  `bbox` is `[x0, y0, x1, y1]` in pixel coordinates of the **source page
  image** the OCR engine ran on. The web app does not currently consume this
  file, but it is part of the bundle's reproducibility contract and is
  checksum-verified.

The `text` field on each `PageEntry` is the searchable string the app indexes.
The reference pipeline derives it from the OCR regions via
`concatenate_reading_order` (top-to-bottom, then left-to-right with a 15-px
line tolerance). A producer that has plain text already (e.g. a born-digital
PDF) can place it directly in `text` and write any valid (possibly empty)
array to the OCR JSON file.

## Original file

A copy of the original source artifact must be present at
`original.<original_format>` at the root of the bundle (e.g. `original.pdf`).
The UI links to it directly as `/bundle/<id>/original.<original_format>`.

The file must be byte-identical to whatever the producer hashed to obtain
`content_hash`. If a producer derives `content_hash` from something other
than this file, that's the producer's responsibility to keep coherent — but
the link in the UI will still point to whatever sits at this path.

## Write order

The reference pipeline writes the bundle as follows; a producer should
follow the same order so the manifest is never observed pointing at missing
files:

1. Create the bundle directory and the `pages/`, `thumbs/`, `ocr/`
   subdirectories.
2. Copy the source to `original.<fmt>`.
3. For each page: write `pages/{NNNN}.webp`, `thumbs/{NNNN}.webp`, and
   `ocr/{NNNN}.json`.
4. Copy the first thumbnail to `cover.webp` (when there is at least one page).
5. Compute checksums by walking the bundle, excluding `manifest.json`.
6. Atomically write the manifest: write to `manifest.json.tmp`, then rename
   to `manifest.json`. The `.tmp` file must not exist when the bundle is
   handed off to the importer.

The discovery step in `bulk_import` keys off `manifest.json` existing
(`magsearch.bulk_import.discover_bundles`), so a half-written bundle with no
manifest is simply skipped.

## Re-import behavior

`import_bundle` is idempotent on identical content. When the importer sees a
manifest whose `id` matches an existing row and whose `content_hash` matches
that row's `content_hash`, it returns immediately without deleting or
re-inserting anything — `ingested_at`, `Page` rows, and any DB-side state
are all preserved.

On a `content_hash` mismatch with an existing `id`, the importer refuses to
proceed (`ImportError("id collision …")`). The producer must pick a
different `id` and re-bundle. There is no force-replace path in the importer;
operators wanting to replace a magazine in place should delete it via the
admin UI first.

## What ends up in the database vs. on disk

For reference, the importer copies the following manifest fields onto the
`Magazine` row:

- `id`, `title`, `issue`, `publication_date`, `publisher`,
  `original_filename`, `original_format`, `page_count`, `content_hash`,
  `cover_path` (with `<id>/` prepended), `ocr_engine`, `ocr_engine_version`.
- `ingested_at` is set to "now" at insert time. The web app's "Recently filed"
  ordering uses this column.

And for each `PageEntry`, the importer writes a `Page` row with
`page_number`, `image_path` and `thumb_path` (both with `<id>/` prepended),
and the HTML-escaped `text`. `ocr_path` is not stored.

Everything else stays on disk and is served by the `/bundle/<path>` route,
which is a thin path-traversal-guarded `FileResponse` over `bundles_root`.

## Minimal manifest example

```json
{
  "schema_version": 1,
  "id": "byte-1985-12",
  "title": "Byte",
  "issue": "Vol 10 No 12",
  "publication_date": "1985-12-01",
  "publisher": "McGraw-Hill",
  "original_filename": "Byte_Vol10_No12_Dec1985.pdf",
  "original_format": "pdf",
  "page_count": 2,
  "content_hash": "3f1c9b…<full 64-hex-char SHA-256 of the original PDF>",
  "ocr_engine": "paddleocr",
  "ocr_engine_version": "3.1.0",
  "cover_path": "cover.webp",
  "pages": [
    {
      "page_number": 1,
      "image_path": "pages/0001.webp",
      "thumb_path": "thumbs/0001.webp",
      "ocr_path": "ocr/0001.json",
      "text": "Editor's note A look at the year ahead in personal computing"
    },
    {
      "page_number": 2,
      "image_path": "pages/0002.webp",
      "thumb_path": "thumbs/0002.webp",
      "ocr_path": "ocr/0002.json",
      "text": "Letters to the editor Readers respond to our November cover story"
    }
  ],
  "checksums": [
    {"path": "cover.webp",        "sha256": "…"},
    {"path": "ocr/0001.json",     "sha256": "…"},
    {"path": "ocr/0002.json",     "sha256": "…"},
    {"path": "original.pdf",      "sha256": "…"},
    {"path": "pages/0001.webp",   "sha256": "…"},
    {"path": "pages/0002.webp",   "sha256": "…"},
    {"path": "thumbs/0001.webp",  "sha256": "…"},
    {"path": "thumbs/0002.webp",  "sha256": "…"}
  ]
}
```

## Validating a bundle without importing it

The cheapest end-to-end check a producer can run is:

```python
from pathlib import Path
from magsearch.manifest import Manifest
from magsearch.importer import _verify_checksums  # see caveat below

bundle = Path("<bundles_root>/byte-1985-12")
manifest = Manifest.model_validate_json((bundle / "manifest.json").read_text())
_verify_checksums(bundle, manifest)
print("OK:", manifest.id, manifest.page_count, "pages")
```

This catches schema violations and any missing/corrupt files but does not
hit the database. `_verify_checksums` is a private helper; if you'd rather
not depend on it, replicate it in ~10 lines: iterate
`manifest.checksums`, assert each file exists, and compare its SHA-256 to
`sha256`.

A full integration check is to call `magsearch.importer.import_bundle` against
a throwaway SQLite database — the importer raises a clear `ImportError` for
every contract violation it knows about.
