# `magsearch delete` CLI — Design

**Date:** 2026-07-16
**Status:** Approved

## Problem

Deleting a magazine from production is only possible through the admin web UI
(`POST /admin/issues/{magazine_id}/delete`), one issue at a time. When a batch
of issues needs removing — e.g. the recently-ingested Computer Gaming World run
produced from a machine with hardware problems — clicking through the UI is
tedious, and there is no way to do it from a Docker `exec` shell where operators
already run `magsearch import` / `magsearch ocr-rescale`.

There is no CLI delete command today.

## Goal

Add a `magsearch delete` command that removes one or more magazines — DB rows,
full-text index entries, and on-disk bundle files — reusing the exact operations
the admin route already performs. Support deleting a whole publication by title
so a multi-issue cleanup is a single command.

## Non-goals

- No new deletion primitives. Reuse `importer.delete_bundle_dir` and the ORM
  cascade / FTS triggers that the admin route relies on.
- No soft-delete / trash / undo. Deletion is immediate and permanent (re-ingest
  restores from the source PDFs, which live outside the bundle).
- No substring/glob title matching. Exact (case-insensitive) title match only,
  to avoid an over-broad selector deleting more than intended.

## Command

```
magsearch delete [IDS...] [--title TEXT] [--yes/-y]
```

| Arg / flag        | Meaning |
|-------------------|---------|
| `IDS` (variadic)  | Zero or more magazine IDs to delete. |
| `--title TEXT`    | Also select every magazine whose `title` equals `TEXT`, case-insensitively (exact match, not substring). |
| `--yes` / `-y`    | Skip the interactive confirmation prompt (for non-interactive Docker exec / scripts). |

IDs and `--title` combine as a **union**, deduplicated by magazine ID. At least
one selector must be supplied.

### Examples

```
magsearch delete computer-gaming-world-1993-04
magsearch delete id-a id-b id-c
magsearch delete --title "Computer Gaming World"
magsearch delete --title "Computer Gaming World" --yes   # scripted, no prompt
```

## Behavior

1. Resolve `settings = get_settings()`, build engine + session factory exactly as
   the `import` command does.
2. **Resolve targets** inside a session:
   - For each ID: `db.get(Magazine, id)`. IDs with no row are collected into a
     `not_found` list.
   - For `--title`: `select(Magazine).where(func.lower(Magazine.title) == text.lower())`.
   - Union the two sets, dedupe by `Magazine.id`, keep a stable order (IDs in the
     order given, then any title-only matches sorted by ID).
3. **Guard rails:**
   - No selector at all (no IDs and no `--title`) → print usage error to stderr,
     `raise typer.Exit(code=2)`.
   - Selectors given but nothing matched → print message, `raise typer.Exit(code=1)`.
   - `not_found` IDs → print a warning line per unknown ID, but proceed with the
     targets that *were* found.
4. **Summary + confirmation:** print the target count, total page count, and total
   on-disk bundle size (sum of file sizes under each bundle dir, shown in MB), then
   list each target (`id — title, issue`). Unless `--yes`, call
   `typer.confirm("Proceed?")`; declining aborts immediately with nothing changed.
5. **Delete (DB-first, then disk):**
   - `db.delete(mag)` for every target, then commit **once**. The ORM
     `cascade="all, delete-orphan"` removes the `pages` rows; the `pages_ad`
     AFTER-DELETE trigger removes their text from `pages_fts`. FK enforcement is
     already enabled on the engine (`_enable_sqlite_fk`).
   - After the commit, call `delete_bundle_dir(settings.bundles_dir, mag.id)` for
     each target. Ordering matches the admin route: a crash between commit and
     rmtree leaves a recoverable stray directory, never a dangling DB row.
6. Print a per-ID result line and a final summary
   (`deleted N magazines (M pages), freed ~X MB`); exit 0.

## Error handling

| Situation | Result |
|-----------|--------|
| No selector supplied | stderr usage message, exit 2 |
| Selectors given, zero matches | message, exit 1 |
| Some IDs unknown, others valid | warn per unknown ID, delete the valid ones, exit 0 |
| `delete_bundle_dir` raises (path guard) | cannot occur for real IDs; if raised, report to stderr and exit 1 |

## Testing (TDD)

Against a temporary SQLite DB + a fixture bundle staged under a temp `bundles_dir`:

1. **Delete by ID** removes the magazine row, its `pages` rows, its `pages_fts`
   entries (a search that previously matched now returns nothing), and the bundle
   directory on disk.
2. **Delete by `--title`** removes all issues sharing that title and leaves a
   non-matching magazine untouched.
3. **Unknown ID** among valid ones: warning emitted, valid targets deleted, unknown
   reported.
4. **No selector** → exit code 2; **no match** → exit code 1.
5. **`--yes`** skips the prompt; **declining** the prompt (simulated input) leaves
   all rows, FTS entries, and files intact.

Tests use Typer's `CliRunner` (following existing CLI tests) with `input=` to drive
the confirm prompt.

## Files touched

- `src/magsearch/cli.py` — add the `delete` command (thin orchestration over
  existing helpers). If target-resolution + sizing grows beyond a small, readable
  block, extract a `delete_magazines(...)` helper into `src/magsearch/importer.py`
  alongside `delete_bundle_dir`, and have the CLI call it — keeping the command
  function focused on argument parsing, output, and confirmation.
- `tests/` — new test module for the command.
- `README.md` — document the command in the CLI reference.
