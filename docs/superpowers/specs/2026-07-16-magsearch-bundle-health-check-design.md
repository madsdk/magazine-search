# `magsearch check` — Bundle Health Check — Design

**Date:** 2026-07-16
**Status:** Approved

## Problem

Bundles ingested on a machine with hardware problems can be silently damaged:
truncated ingestion (missing page/thumb/OCR files), bit-rot / corruption
(undecodable images, files whose bytes no longer match their recorded sha256),
empty OCR (a page with zero recognized text), and misaligned OCR (legacy
flat-array bboxes in the original render resolution instead of displayed-image
coordinates — the highlight-misalignment bug already diagnosed on Computer
Gaming World).

Today there is no way to detect any of this short of opening pages in the web
UI one at a time. Operators need a single command they can run from the Docker
`exec` shell — the same place they run `import` / `ocr-rescale` / `delete` — to
audit one bundle, a publication, or the whole corpus, and get a clear
pass/fail with the specific problems listed.

## Goal

Add a `magsearch check` command that audits bundles against the failure modes
above by cross-checking three sources of truth: the on-disk files, the bundle
`manifest.json`, and the database (`magazines` / `pages` rows and the
`pages_fts` search index). It reports problems with severity and returns a
non-zero exit code when anything is wrong, so it works in scripts and
monitoring.

## Non-goals

- **No repair.** `check` only reports. `magsearch ocr-rescale` and re-ingest
  remain the fixers; each finding names the fix where one exists.
- **No JSON output.** Human-readable report plus exit code only. A `--json`
  flag can be added later if a monitor needs structured output.
- **No new deletion / mutation.** The command opens the DB read-only in intent
  (it issues no writes except the FTS `'integrity-check'` command, which does
  not modify data).

## Command

```
magsearch check [IDS...] [--title TEXT] [--checksums] [--strict]
```

| Arg / flag      | Meaning |
|-----------------|---------|
| `IDS` (variadic)| Zero or more magazine IDs to check. |
| `--title TEXT`  | Also select every magazine whose `title` equals `TEXT`, case-insensitively (exact match, not substring) — same semantics as `delete`. |
| `--checksums`   | Additionally recompute every file's sha256 and compare to `manifest.checksums`. Off by default (reads every byte). |
| `--strict`      | Treat warnings as failures when computing the exit code. |

**Selection.** `IDS` and `--title` combine as a union, deduplicated by
magazine ID — identical to `delete`, reusing `resolve_magazines`.

**No selector → whole-corpus audit.** When neither `IDS` nor `--title` is
given, the target set is the **union of all DB `Magazine` IDs and every
subdirectory under `bundles_dir`**. Using the union (not just the DB) is what
lets the check flag orphans in both directions (see below).

### Examples

```
magsearch check computer-gaming-world-1993-04
magsearch check --title "Computer Gaming World"
magsearch check --title "Computer Gaming World" --checksums
magsearch check                      # audit every bundle
magsearch check --checksums --strict # full audit, warnings fail the exit code
```

## Checks & severity

The `manifest.json` is the on-disk source of truth for what a bundle should
contain. The DB is cross-checked against it. Findings are `ERROR` or `WARNING`.

### ERROR — bundle is unhealthy (non-zero exit)

- **Manifest missing or unparseable.** A bundle directory with no readable
  `manifest.json` is an incomplete/aborted ingest (the pipeline deliberately
  leaves no manifest when a `FatalOCRError` aborts a run). No further per-file
  checks run for that bundle; it is reported as a single error.
- **Missing file.** Any manifest-listed file absent on disk:
  `pages/NNNN.webp`, `thumbs/NNNN.webp`, `ocr/NNNN.json` for each page; plus
  `cover.webp` (when `manifest.cover_path` is non-empty) and
  `original.<original_format>`.
- **Undecodable image** *(image-decode, on by default).* Any page image,
  thumbnail, or cover that fails to open or reports zero width/height.
- **Checksum mismatch** *(only with `--checksums`).* A file whose recomputed
  sha256 differs from its `manifest.checksums` entry, or a checksummed file
  that is missing (also covered above).
- **Unparseable OCR JSON.** An `ocr/NNNN.json` that is not valid JSON.
- **DB drift.** The `Magazine` row is missing, its `page_count` differs from
  the manifest's, or the set of `Page.page_number` values differs from the
  manifest's page numbers.
- **Orphan** *(whole-corpus mode only).* A bundle directory on disk with no DB
  `Magazine` row, or a DB `Magazine` row with no bundle directory. Reported
  once per orphan, attributed to the missing side.
- **FTS integrity failure.** FTS5's built-in `'integrity-check'` reports the
  `pages_fts` index is inconsistent with the `pages` content table. Run once
  per invocation (whole-index, not per bundle) and reported as a run-level
  error.

### WARNING — reported; fails the exit code only under `--strict`

- **Legacy OCR format.** An `ocr/NNNN.json` in the legacy flat-array shape
  (a JSON array rather than the `{"width","height","regions":[...]}` object).
  Legacy files hold un-rescaled coordinates and cause misaligned highlights.
  Message names the fix: run `magsearch ocr-rescale`.
- **OCR bboxes out of bounds.** In a new-format OCR file, a region whose bbox
  extends beyond the recorded `width`/`height` by more than a small tolerance
  — a sign of bad coordinates.
- **Empty OCR page.** An OCR file with zero text regions. May be a legitimately
  blank page or an OCR failure (e.g. page 114 of the damaged run); flagged so a
  human can judge.
- **DB text drift.** A `Page.text` value that differs from the manifest's text
  for that page — the DB is out of sync with the bundle.

### Why `--checksums` is opt-in

File presence, OCR sanity, and image decode are cheap enough to run across the
whole corpus by default. Recomputing sha256 over every byte of every webp
(gigabytes across the archive) takes minutes, so full checksum verification is
behind `--checksums`. All four check categories from the design discussion are
implemented; the flag only controls whether the byte-level pass runs.

## Behavior

1. Resolve `settings = get_settings()`, build engine + session factory exactly
   as `import` / `delete` do.
2. **Resolve targets** inside a session:
   - With `IDS` and/or `--title`: use `resolve_magazines`; report `not_found`
     IDs as errors but proceed with the rest.
   - With no selector: build the union of all DB `Magazine` IDs and all
     subdirectory names under `settings.bundles_dir`.
   - If the resolved target set is empty, print a message and exit 1.
3. **Run the FTS integrity check once** (run-level), recording a run-level
   error on failure.
4. **For each target**, produce a `BundleReport`:
   - Load `manifest.json`; on missing/unparseable, emit the manifest error and
     stop that bundle's per-file checks.
   - Run presence, OCR-sanity, image-decode, and (if `--checksums`) checksum
     checks against the manifest's page list and file set.
   - Load the DB `Magazine` + `Page` rows for the ID and run the DB-drift and
     text-drift checks.
   - For whole-corpus mode, emit orphan errors for IDs present on only one side.
5. **Print** each bundle's header (`id — title №issue (N pages)`) followed by
   its findings (`LEVEL  page  path — message`), or `: OK` when clean. End with
   a summary line: bundles checked, OK count, unhealthy count, total errors and
   warnings.
6. **Exit code:** `0` if every bundle is healthy; `1` if any bundle has an
   error (or, under `--strict`, any warning) or the FTS check failed; `2` for
   usage errors (none reached here — selection is always valid) and DB-open
   failures.

## Error handling

| Situation | Result |
|-----------|--------|
| No selector | whole-corpus audit (not an error) |
| Selector given, zero targets resolved | message, exit 1 |
| Some IDs unknown, others valid | error per unknown ID, check the valid ones, exit 1 |
| Manifest missing/unparseable for a bundle | one error for that bundle, skip its per-file checks, continue |
| Any bundle unhealthy | exit 1 (or per `--strict` for warnings) |
| DB cannot be opened | stderr message, exit 2 |

## Architecture / files

Keep the check logic in a dedicated, unit-testable module and keep the CLI
command a thin orchestrator (mirroring how `delete` reuses `importer` helpers).

- **Create `src/magsearch/health.py`:**
  - `Finding` dataclass: `level` (`"error"`/`"warning"`), `page` (int | None),
    `path` (str | None, bundle-relative), `message` (str).
  - `BundleReport` dataclass: `magazine_id`, `title`, `issue`, `page_count`,
    `findings: list[Finding]`, with `errors`/`warnings` counts and an `ok`
    property.
  - `check_bundle(bundles_dir, magazine_id, manifest, mag_row, page_rows, *,
    verify_checksums=False, decode_images=True) -> BundleReport` — pure over
    its inputs (manifest already parsed, DB rows already fetched), so it is
    unit-testable without a live DB or CLI.
  - `check_fts_integrity(session) -> Finding | None` — runs the FTS5
    `'integrity-check'` and returns a run-level error finding on failure.
  - `iter_all_target_ids(session, bundles_dir) -> list[str]` — the DB ∪ disk
    union for whole-corpus mode (sorted, deduped).
- **Modify `src/magsearch/cli.py`:** add the `check` command — argument
  parsing, target resolution (reusing `resolve_magazines` and
  `iter_all_target_ids`), per-bundle manifest + DB-row loading, calling
  `check_bundle`, printing the report, and computing the exit code.
- **Modify `src/magsearch/importer.py`** *(only if needed)*: reuse
  `resolve_magazines` as-is; the union helper lives in `health.py`.
- **Create `tests/test_health.py`:** unit tests per check against staged
  fixture bundles + in-memory/temp DB rows — missing file, undecodable image,
  checksum mismatch, unparseable OCR, legacy-format warning, out-of-bounds
  bbox warning, empty-OCR warning, DB page_count / page-number drift, DB text
  drift, and a fully-clean bundle producing an empty finding list.
- **Create `tests/test_cli_check.py`:** end-to-end `CliRunner` tests — clean
  bundle exits 0; a bundle with a missing file exits 1; `--checksums` catches a
  corrupted file; `--strict` makes a warning-only bundle exit 1 while the
  default exits 0; whole-corpus mode reports an orphan; unknown ID is reported
  and exits 1.
- **Modify `README.md`:** document `check` in the CLI reference next to
  `delete` and `ocr-rescale`.

## Testing (TDD)

Against a temporary SQLite DB plus fixture bundles staged under a temp
`bundles_dir` (following `tests/test_cli_delete.py`):

1. **Clean bundle** → no findings, exit 0.
2. **Missing page file** → error, exit 1.
3. **Undecodable image** (truncated/garbage webp) → error, exit 1.
4. **Checksum mismatch** (file mutated after manifest) → not flagged without
   `--checksums`; flagged as error with `--checksums`.
5. **Legacy-format OCR** → warning; exit 0 by default, exit 1 with `--strict`.
6. **Empty-OCR page** → warning.
7. **Out-of-bounds bbox** (new format) → warning.
8. **DB page_count / page-number drift** → error.
9. **DB text drift** → warning.
10. **Whole-corpus orphan** (bundle dir with no DB row; DB row with no dir) →
    error in each direction.
11. **FTS integrity** → clean index yields no run-level error (a corruption
    case is covered if feasible to construct; otherwise the clean path is
    asserted).

Tests use Typer's `CliRunner` for the CLI layer and direct calls to
`check_bundle` / `check_fts_integrity` for the unit layer.
