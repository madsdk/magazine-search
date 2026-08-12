# `magsearch drop-leading-pages` CLI — Design

**Date:** 2026-08-12
**Status:** Approved

## Problem

A number of CBR issues have been ingested and imported whose archives contain an
extra image ahead of the magazine cover — a scan-credits sheet, release-group
logo, or advertisement. `formats._read_cbr` sorts archive members by name and
numbers them from 1, so that extra image becomes page 1 of the bundle and the
real cover becomes page 2. Every page is off by one, and `cover.webp` (a copy of
`thumbs/0001.webp`) shows the junk image everywhere the UI displays a cover.

This is bad input, not a pipeline bug: given an archive whose first member is
junk, the pipeline did the correct thing. The OCR text of the real pages is
correct — it sits at the wrong page number.

There is no way to repair an imported bundle today. `import_bundle` is a no-op
when `id` and `content_hash` both match an existing row (`importer.py:83-91`), so
re-importing a hand-edited bundle changes nothing, and `magsearch delete` removes
the on-disk bundle directory along with the DB rows — destroying the files that
would be repaired.

## Goal

Add a `magsearch drop-leading-pages` command that removes the leading junk
page(s) from one or more already-imported bundles: renumbers the surviving pages
on disk, rebuilds the cover and manifest, and updates the database to match — in
one operation, without re-running OCR.

## Non-goals

- **No re-OCR.** The surviving pages' images and OCR JSON are already correct;
  only their page numbers change.
- **No change to `original.<fmt>`.** The source archive stays byte-identical,
  junk image included, so `content_hash` — the dedupe key — stays stable and the
  bundle id remains valid. See [Accepted consequences](#accepted-consequences).
- **No detection heuristics.** The operator names the bundles to fix. Nothing is
  guessed about which page is junk.
- **No arbitrary page removal.** Only a contiguous run from the front. A command
  that can delete page 47 of an imported magazine is a bigger blast radius than
  this problem needs.
- **No delete / re-import round trip.** It would issue new `Page.id`s, dropping
  every researcher's saved page for those issues, and reset `ingested_at`.

## Command

```
magsearch drop-leading-pages [IDS...] [--count N] [--dry-run] [--yes/-y]
```

| Arg / flag       | Meaning |
|------------------|---------|
| `IDS` (variadic) | One or more magazine IDs to repair. At least one required. |
| `--count N`      | Number of leading pages to drop from each named bundle. Default `1`. |
| `--dry-run`      | Report what would be dropped and stop. No disk or DB writes. |
| `--yes` / `-y`   | Skip the interactive confirmation prompt (for scripted Docker `exec`). |

`--count` applies to every id in the invocation. A bundle needing two pages
dropped is a separate invocation from those needing one.

### Examples

```
magsearch drop-leading-pages --dry-run zzap-64-1985-05
magsearch drop-leading-pages zzap-64-1985-05 zzap-64-1985-06
magsearch drop-leading-pages --count 2 crash-1986-01 --yes
```

### Dry-run output

The dry run is the safety check that the page about to be removed is junk and
not the cover, so it shows the OCR text that page carries:

```
zzap-64-1985-05 — Zzap!64 №5 (116 pages → 115)
  drop page 1  pages/0001.webp  "Scanned by RETRO-SCANS 2019 — visit us at ..."
  new page 1 ← old page 2  thumbs/0002.webp → thumbs/0001.webp (new cover)
```

Page text is read from the manifest's `PageEntry.text`, truncated to 80
characters on a whitespace boundary, control characters stripped. When the page
has no OCR text, print `"(no text)"` — an empty result is meaningful here (it is
what a logo sheet looks like), so it must be distinguishable from a failure to
read.

The same summary prints before the confirmation prompt in a real run.

## Modules

New `src/magsearch/bundle_edit.py`, holding the repair logic with no Typer
imports — the same split `importer.py` and `health.py` already use, so the logic
is testable without a CLI runner.

```python
@dataclass
class DropPlan:
    bundle_dir: Path
    magazine_id: str
    count: int
    dropped: list[PageEntry]      # manifest entries being removed, in order
    surviving: list[PageEntry]    # remaining entries, old numbering
    new_page_count: int

def plan_drop(bundle_dir: Path, count: int) -> DropPlan
def apply_drop(plan: DropPlan) -> Manifest       # disk only; returns new manifest
def resync_magazine(session: Session, manifest: Manifest, count: int) -> None
```

`plan_drop` performs every refusal check (below) and reads nothing it does not
need, so `--dry-run` is exactly "call `plan_drop`, print, stop".

New `src/magsearch/checksums.py`, holding the two helpers that are currently
private and duplicated:

```python
def collect(bundle_dir: Path) -> list[FileChecksum]
def verify(bundle_dir: Path, manifest: Manifest) -> None   # raises ChecksumError
```

`pipeline._collect_checksums` and `importer._verify_checksums` become thin
delegating aliases rather than being deleted: `docs/datamodel/bundles.md:318`
documents `importer._verify_checksums` as the bundle-validation entry point for
third-party producers, and the private name in `pipeline` keeps that module's
diff small. `importer.ImportError` remains what `import_bundle` raises, so its
contract is unchanged; `verify` raising `ChecksumError` is wrapped there.

## Refusal checks

`plan_drop` raises `BundleEditError` — the command reports it, skips that bundle,
and continues to the next id — when:

| Condition | Reason |
|-----------|--------|
| `count < 1` | Nothing to do; a typo, not an instruction. |
| Bundle directory or `manifest.json` missing | Nothing to repair. |
| Manifest fails schema validation | Repairing an invalid manifest means guessing. |
| `count >= len(manifest.pages)` | Would empty the bundle. Deleting a magazine is `magsearch delete`'s job. |
| Page numbers are not exactly `1..N` | Renumbering by subtraction assumes contiguity from 1. |
| Checksums do not verify before the edit | The bundle is already damaged; repairing it would bake the damage into a fresh manifest and hide it. |
| A file referenced by a surviving page entry is missing | Same. (Covered by the checksum check, which requires presence.) |

The checksum pass is the expensive one — it hashes every file in the bundle,
including a 60 MB+ original. That cost is accepted: it runs once per repaired
bundle, and it is the only thing standing between a silent pre-existing
corruption and a manifest that certifies it as correct.

If the bundle repairs cleanly but has no `Magazine` row, the disk repair still
commits and the command warns `not in database — run 'magsearch import'`. The
bundle is now correct; refusing would leave it broken for no gain.

## Disk repair

`apply_drop` never mutates the live bundle. It builds a sibling staging
directory and swaps it in:

1. Create `<bundles_root>/<id>.new/` with `pages/`, `thumbs/`, `ocr/`.
2. **Hardlink** each surviving page's `image_path`, `thumb_path` and `ocr_path`
   into the staging tree under its new stem: a page at old number `k` becomes
   `{k - count:04d}`, keeping the original file's directory and suffix. Hardlink
   `original.<fmt>` unchanged. Hardlinks make staging near-free in time and disk
   no matter how large the original is, and the source files are never written
   through — the link and the original are the same immutable inode.
3. Write a fresh `cover.webp` from the new `thumbs/0001.webp` via
   `normalize.write_cover`. Never hardlink the old cover: it is a copy of the
   junk thumbnail, which is the whole defect.
4. Compute `checksums` over the staging tree with `checksums.collect`.
5. Write the manifest to `<id>.new/manifest.json.tmp` and rename it into place.
   `schema_version`, `id`, `title`, `issue`, `publication_date`, `publisher`,
   `original_filename`, `original_format`, `content_hash`, `ocr_engine`,
   `ocr_engine_version` are copied verbatim; `page_count`, `pages` and
   `checksums` are rewritten; `cover_path` stays `"cover.webp"`.
6. Swap: rename `<id>` → `<id>.old`, `<id>.new` → `<id>`, then `rmtree` the old
   directory. Deleting `<id>.old` does not touch the surviving files' contents —
   the hardlinks in the live bundle hold those inodes.

Every step before 6 is invisible to a reader of the bundle. An interruption
leaves a stray `<id>.new/` (or `<id>.old/`) and a completely intact live bundle;
re-running the command starts over. Staging directories are removed on failure,
and a pre-existing `<id>.new` or `<id>.old` is a refusal, not something to
clobber — it means a previous run died and the operator should look at it.

Path safety: the staging and swap targets are resolved and asserted to be
immediate children of `bundles_root`, matching the guard in
`importer.delete_bundle_dir`.

## Database resync

`resync_magazine` takes the rewritten manifest and brings the rows in line:

1. Delete the `Page` rows for `page_number <= count` via `session.delete`, so the
   ORM emits per-row `DELETE`s and the `pages_ad` trigger fires for each.
2. Shift the survivors in **two passes**: `page_number → -page_number`, then
   `-page_number - count`. `uq_pages_mag_page` is enforced per row and SQLite
   guarantees no ordering for a bulk `UPDATE`, so a single `page_number =
   page_number - count` can transiently collide with a not-yet-updated row. The
   negative range cannot collide with any live value.
3. Set each survivor's `image_path` and `thumb_path` from the new manifest entry
   for its new page number, `<id>/` prefixed exactly as `import_bundle` does.
4. Set `Magazine.page_count = len(manifest.pages)` and
   `cover_path = f"{manifest.id}/{manifest.cover_path}"`.

`Page.text` is never touched and surviving `Page.id`s never change. Two things
follow:

- **`pages_fts` stays consistent with no explicit work.** The `pages_au` trigger
  fires on the shift and re-inserts the same rowid with the same text — a net
  no-op against the external-content index. The dropped page leaves the index
  through `pages_ad`.
- **Research saves survive.** `research_topic_pages.page_id` references
  `pages.id`, which is stable for every surviving page. Saves pointing at the
  dropped junk page cascade away, which is correct.

`ingested_at` is left alone, so "Recently filed" ordering does not churn.

## Transaction ordering

Per bundle: build the staging directory, apply the DB changes and `flush()`,
perform the directory swap, then `commit()`.

Flushing before the swap means a constraint violation or ORM error rolls the DB
back while the live bundle is still untouched — the staging directory is removed
and the bundle is reported as skipped. Committing after the swap keeps the window
in which disk and DB disagree down to the commit itself. Should the process die
in that window, the result is a repaired bundle with a stale DB — which
`magsearch check` reports as a `page_count` mismatch (`health.py:178-180`), and
which re-running the
command refuses (page numbers on disk are already `1..N-1`) rather than
double-dropping.

Each bundle gets its own session scope, so one failure does not roll back
already-repaired bundles.

## `ocr-rescale` guard

After a repair, bundle page `N` corresponds to archive page `N + count`.
`magsearch ocr-rescale` (`cli.py:488`) pairs `read_pages(original, fmt)` output
with `NNNN` stems positionally, so on a repaired bundle it would rescale each
page's bboxes against the wrong source image's dimensions — silently, since the
scale factors are plausible numbers.

Add a pre-flight to `ocr_rescale_cmd`: compare `formats.page_count(original,
fmt)` with `manifest.page_count`, and when they differ, skip the bundle with

```
! <id>: manifest has 115 pages but original.cbr has 116 — page numbering does
  not match the archive (dropped pages?); skipping
```

counted as a skip rather than a failure. This is a guard against a real
mismatch, not bookkeeping about this command specifically: any bundle whose page
numbering has diverged from its archive is unsafe to rescale positionally.

## Testing

Unit tests against `tests/fixtures/bundles.py::make_bundle`:

- Drop 1 of 3: files renumbered, junk files gone, `page_count` and `pages[]`
  correct, `checksums.verify` passes on the result, `cover.webp` is byte-equal to
  the new `thumbs/0001.webp`.
- `import_bundle` accepts the repaired bundle into a fresh DB — the manifest
  contract still holds end to end.
- `--count 2` drops two and renumbers by two.
- Each refusal case leaves the bundle byte-identical: `count >= page_count`,
  non-contiguous page numbers, a corrupted file failing pre-verification, a
  pre-existing `<id>.new`, a missing manifest.
- Non-canonical page paths (a manifest using `img/p1.png`) renumber within their
  own directory and suffix.
- Induced failure between staging and swap (monkeypatched `Path.rename`) leaves
  the bundle byte-identical and removes the staging directory.

Database tests:

- Surviving `Page.id`s are unchanged; `page_number`, `image_path`, `thumb_path`
  match the new manifest; `Magazine.page_count` and `cover_path` updated.
- A `ResearchTopicPage` saved against old page 3 still resolves to the same
  `Page` at new page 2; one saved against old page 1 is gone.
- FTS: a term unique to old page 3 is found at new page 2; a term unique to the
  dropped page returns nothing; `health.check_fts_integrity` reports clean.
- A DB failure mid-resync leaves both the DB and the bundle untouched.

CLI tests (`tests/test_cli.py` style):

- `--dry-run` prints the plan including page text and writes nothing.
- Confirmation prompt aborts cleanly; `--yes` skips it.
- An unknown id is reported and the remaining ids still process.
- A bundle with no `Magazine` row is repaired on disk with a warning.

Plus an `ocr-rescale` test that a page-count mismatch skips the bundle.

## Operational procedure

```
magsearch drop-leading-pages --dry-run <id>...     # confirm the junk page
magsearch drop-leading-pages <id>...               # repair
magsearch check --checksums <id>...                # confirm manifest ↔ DB ↔ files
```

Then spot-check a cover in the web UI. No restart is needed — the web layer reads
bundle files per request and holds no page cache.

## Accepted consequences

Leaving `original.<fmt>` untouched is a deliberate trade, and it costs two
things:

1. The UI's link to `/bundle/<id>/original.cbr` still serves an archive whose
   first image is the junk page. The archived source stays a faithful copy of
   what was ingested, which is the point.
2. Bundle page numbering no longer matches archive page numbering. The
   `ocr-rescale` guard above is what keeps that from becoming a silent
   correctness bug; any future tool that pairs bundle pages with archive pages
   positionally needs the same check.

Repacking the archive without the junk page was considered and rejected for now:
`rarfile` can only extract, so it would mean rewriting each CBR as a CBZ, which
changes `content_hash`, `original_format` and `original_filename` — a much larger
change to the bundle's provenance than the defect warrants.
