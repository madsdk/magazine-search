# Single-issue bundle upload via the web UI

## Problem

Today, adding one new magazine issue takes four steps across two machines:

1. SSH to the GPU box, run `magsearch ingest <file> …` — produces `bundles/<id>/`.
2. `rsync` the bundle to the server.
3. SSH to the server, run `magsearch import <bundle>`.
4. The web app picks it up.

The bulk flow (`bulk-ingest` → `rsync` → `bulk-import` with sidecar metadata and
resumable state logs) is well-suited to importing many issues at once and is
explicitly out of scope for this design. But for a single issue, the
four-step CLI dance is heavier than it needs to be.

## Goal

Add a third entry point into the existing importer: an admin web page that
accepts a `.zip` of a bundle directory, validates it, places it under
`bundles_dir/`, and runs `import_bundle()` — collapsing steps 2–4 into a single
upload click.

Non-goals:

- No web-based ingestion (OCR stays on the GPU box; the server has no GPU).
- No bulk web upload (the existing bulk flow stays as is).
- No metadata form on the web side — all metadata is in the bundle's
  `manifest.json` already.
- No new CLI command for producing the zip — users run `zip -r` themselves.

## Architecture

The new flow alongside the existing CLI flow:

```
GPU box (unchanged):
  magsearch ingest mag.pdf --title …   → bundles/byte-1985-12/
  cd bundles && zip -r byte-1985-12.zip byte-1985-12/

Server (web UI, new):
  /admin/issues
    [Upload bundle] button ──────────► /admin/issues/upload  (GET: form)
                                              │
                                              │ POST .zip
                                              ▼
                                       handler:
                                         1. stream upload to temp file
                                         2. extract to temp dir
                                         3. validate (manifest.json,
                                            page files present, checksums)
                                         4. atomic rename → bundles_dir/<id>/
                                         5. import_bundle() into DB
                                       ────────────────────────────────
                                              │
                              success ───────►│◄────── failure
                                              │            │
                              redirect to     │       re-render form
                              /admin/issues/<id>      with error
                              (the new issue)         (no side effects
                                                      left on disk)
```

Invariants:

- The bundle format and `import_bundle()` are reused as-is. No changes to the
  cross-machine contract.
- The web upload is a third entry point into the same import path, alongside
  `magsearch import` and `magsearch bulk-import`.
- Failed uploads leave no residue: temp file deleted, temp extraction dir
  deleted, no partial bundle in `bundles_dir/`.
- Success is atomic: the bundle either appears fully in `bundles_dir/` and as
  a DB row, or not at all.

## Web routes & form

Two routes added to `src/magsearch/web/routes_admin.py`:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/admin/issues/upload` | Render the upload form |
| `POST` | `/admin/issues/upload` | Handle the multipart upload + import |

Both inherit `require_admin` from the existing `/admin` router. The POST also
uses `require_csrf` like other admin form posts.

Template: new file `src/magsearch/web/templates/admin/issue_upload.html`.

Form fields:

- `bundle` — `<input type="file" name="bundle" accept=".zip" required>`
- `_csrf` — hidden CSRF token (existing pattern)

No metadata fields. All metadata lives in the bundle's `manifest.json`.

Progress bar: a small inline script hijacks form submit, sends the file via
`XMLHttpRequest`, and writes upload progress into a `<progress>` element. ~20
lines of vanilla JS, no framework. If JS is disabled the form falls back to a
plain multipart submit (no progress bar, but the upload still works).

Issues page (`admin/issues.html`) gets a single `<a href="/admin/issues/upload">Upload
bundle</a>` next to the search field.

## Upload handler

New module `src/magsearch/web/bundle_upload.py` holds the pure logic so it can
be tested without the FastAPI layer; the route in `routes_admin.py` is a thin
wrapper.

```python
class BundleUploadError(Exception):
    """Raised when an uploaded zip is rejected. Message is safe to display."""

def extract_and_stage(
    zip_path: Path,        # uploaded .zip on disk
    bundles_dir: Path,     # final destination root
    *,
    max_uncompressed_bytes: int,
) -> Path:                 # returns the staged bundle dir path
    """Validate and extract a bundle zip into bundles_dir/<id>/.

    Atomic-on-success, no-residue-on-failure. Does NOT touch the DB.
    """
```

Algorithm:

1. **Open the zip** (`zipfile.ZipFile`). Reject if not a valid zip.
2. **Resolve the bundle root inside the zip.** Inspect entries:
   - If `manifest.json` is at the zip root → root is the zip itself.
   - Else if there is exactly one top-level directory and it contains
     `manifest.json` → that dir is the root.
   - Otherwise → `BundleUploadError`.
3. **Zip-bomb / traversal guard.**
   - Sum of `ZipInfo.file_size` ≤ `max_uncompressed_bytes`. Reject if larger.
   - Every entry's resolved path must stay under the extraction temp dir.
     Reject `..` or absolute paths.
4. **Parse `manifest.json`** before extracting anything else (read it with
   `ZipFile.read`). Validate with the existing `Manifest` model. Extract
   `manifest.id`.
5. **Collision pre-check.** If `bundles_dir / manifest.id` already exists:
   - Same `content_hash` → return that path (idempotent re-upload). Skip
     extraction.
   - Different `content_hash` → `BundleUploadError("bundle id … already
     exists with different content")`.
6. **Extract to a temp dir** at `bundles_dir / f".upload-{manifest.id}-{uuid}"`,
   stripping the zip-internal prefix so the temp dir contains `manifest.json`,
   `pages/`, etc. directly.
7. **Verify the extracted bundle.** For each `FileChecksum` in the manifest,
   recompute and compare. Reject on mismatch.
8. **Atomic publish.** `os.rename(temp_dir, bundles_dir / manifest.id)`. Same
   filesystem → atomic on POSIX. Fail loudly if rename crosses filesystems.
9. **Return** the published bundle path.

The route in `routes_admin.py` then calls `import_bundle(staged_path, db,
settings.bundles_dir)` (the existing function) and redirects.

Failure cleanup: a `try/finally` removes the uploaded temp zip and (if
extraction started but didn't reach step 8) the temp extraction dir. The
successful `os.rename` consumes the temp dir, so finally has nothing to clean
up after success.

The upload itself streams to a real temp file (not RAM) using
`shutil.copyfileobj(upload_file.file, temp)`, so a 1 GB upload doesn't OOM
the server.

New setting: `MAGSEARCH_MAX_UPLOAD_BYTES`, default 2 GB. Surfaced through
`Settings`. Caps both multipart body and uncompressed bundle size.

## Zip format & validation contract

Two zip layouts are accepted, because both fall out naturally of `zip -r`:

```
# Shape A: bundle dir is a top-level entry in the zip  (recommended)
byte-1985-12.zip
└── byte-1985-12/
    ├── manifest.json
    ├── original.pdf
    ├── cover.webp
    ├── pages/0001.webp, 0002.webp, …
    ├── thumbs/0001.webp, …
    └── ocr/0001.json, …

# Shape B: bundle contents are at the zip root
byte-1985-12.zip
├── manifest.json
├── original.pdf
├── cover.webp
├── pages/…
├── thumbs/…
└── ocr/…
```

The README recommends Shape A (`cd bundles && zip -r byte-1985-12.zip
byte-1985-12/`). Shape B is accepted because `cd byte-1985-12 && zip -r
../byte-1985-12.zip .` is also a natural reflex.

Rejection cases (each maps to a specific error shown in the form):

| Rejection | Error message |
|---|---|
| Not a zip / corrupt | `file is not a valid zip archive` |
| `manifest.json` not findable | `manifest.json not found at zip root or in a single top-level folder` |
| `manifest.json` doesn't parse | `manifest.json is invalid: <pydantic error>` |
| Sum of uncompressed sizes > limit | `bundle would exceed max size of {N} MB` |
| Path traversal entry | `zip contains unsafe path: {name!r}` |
| ID exists, different content | `bundle id {id!r} already exists with different content` |
| Checksum mismatch on `pages/0042.webp` | `checksum mismatch: pages/0042.webp` |
| Manifest references missing file | `file listed in manifest is missing: {path}` |
| Rename crosses filesystems (server config error) | `cannot publish bundle: temp dir and bundles_dir are on different filesystems` |

Deliberately not validated (out of scope):

- That `original.<ext>` matches a hash beyond what the manifest records — the
  manifest's checksums are the source of truth.
- That OCR JSON parses page-by-page — the importer/server will fail loudly on
  first access if it doesn't.
- That images decode — same reasoning.

Idempotency: re-uploading the same zip is a no-op. Step 5 spots the existing
bundle with matching `content_hash` and short-circuits before extraction. The
handler still calls `import_bundle()`, which is already idempotent. The UI
shows "Bundle already imported. Showing the existing issue." and redirects to
the issue.

## Error handling

Every `BundleUploadError` re-renders the upload form with the message in the
existing error-box pattern (same as `admin/user_form.html`).

HTTP status codes:

- 400 — validation failure (most rejection cases above)
- 413 — request body exceeds `MAGSEARCH_MAX_UPLOAD_BYTES`
- 500 — unexpected internal error

Unexpected exceptions (zip lib crash, disk full, etc.) get caught at the route
boundary and shown as `Upload failed: <type>. Check server logs.` Tracebacks
go to the server log via the existing FastAPI exception path. Temp files are
cleaned up via the `try/finally` regardless.

The form does not preserve the file picker state across errors — browsers
disallow pre-filling `<input type=file>`. The user re-picks the file. This is
a browser constraint, not something to paper over.

## Tests

New file `tests/web/test_bundle_upload.py`.

Pure logic (no FastAPI, `extract_and_stage` direct):

- Shape A zip → bundle published at expected path.
- Shape B zip → identical outcome.
- Re-upload identical zip → idempotent, returns existing path, no temp
  residue.
- Re-upload with same id but different `content_hash` → `BundleUploadError`,
  no residue.
- Corrupt zip → `BundleUploadError`, no residue.
- Manifest missing → `BundleUploadError`.
- Zip with `../foo` entry → `BundleUploadError`, no residue.
- Zip exceeding `max_uncompressed_bytes` → `BundleUploadError`.
- Checksum mismatch in a page → `BundleUploadError`, no residue.
- Manifest references file not present in zip → `BundleUploadError`.

Route layer (FastAPI `TestClient`):

- GET `/admin/issues/upload` as admin → 200, form renders.
- GET as non-admin → existing auth behavior (redirect to login).
- POST valid zip as admin → 303 redirect to `/admin/issues/<id>`, magazine
  row exists in DB.
- POST with missing CSRF → 403 (existing pattern).
- POST with rejected zip → 400, form re-rendered with the error message.
- POST with idempotent re-upload → 303 redirect to existing issue, only one
  DB row in the database.

Reuse existing helpers: `FakeOCREngine` to build a real bundle in a tmp dir
during test setup, then `shutil.make_archive` to zip it. Hand-craft
pathological zips with `zipfile.ZipFile(..., 'w')` for rejection cases.

## Documentation

Three additions to `README.md`:

1. New short section "Single-issue upload via the web UI" placed between
   "Ingesting magazines" and "Bulk ingestion":
   - The flow: ingest on GPU box → `zip -r` → upload at
     `/admin/issues/upload`.
   - The exact `zip -r` invocation that produces the recommended Shape A.
   - Note that `magsearch import` still works and is preferred for bulk.
2. New row in the "Configuration" env-var table:
   - `MAGSEARCH_MAX_UPLOAD_BYTES` — default 2 GB. Caps both multipart body
     and uncompressed bundle size.
3. One-line mention under "Authentication & admin" that bundle uploads
   require an admin session.

## Known limitations / follow-ups

Recorded for later, matching the README's existing "Known limitations" style:

- No upload UI on the GPU box itself — users still type `zip -r` after
  `magsearch ingest`.
- No bulk web upload — many issues at once still goes through `bulk-ingest`
  + `rsync` + `bulk-import`.
- No drag-and-drop UI. Just a file picker. Trivially upgradable later; not
  worth a JS dependency now.
