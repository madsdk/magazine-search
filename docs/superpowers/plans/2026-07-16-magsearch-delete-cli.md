# `magsearch delete` CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `magsearch delete` command that removes one or more magazines (DB rows, full-text index entries, and on-disk bundle files) by ID and/or by exact title, with a confirmation prompt that `--yes` skips.

**Architecture:** A small pure resolver (`resolve_magazines`) in `importer.py` turns IDs + an optional title into a deduped list of `Magazine` rows plus a list of unknown IDs. The `delete` CLI command in `cli.py` wires settings → engine → session, calls the resolver, prints a summary, confirms, then deletes DB rows (ORM cascade + FTS triggers do the rest) and removes bundle directories via the existing `delete_bundle_dir`. DB-first, then disk — matching the admin route.

**Tech Stack:** Python 3.12, Typer (CLI), SQLAlchemy 2.x ORM, SQLite + FTS5, pytest + `typer.testing.CliRunner`.

## Global Constraints

- Reuse existing primitives: `magsearch.importer.delete_bundle_dir` (path-traversal guarded), the ORM `cascade="all, delete-orphan"` on `Magazine.pages`, and the `pages_ad` FTS delete trigger. Add no new deletion primitives.
- DB-first ordering: commit DB deletions before removing any files, so a crash leaves at most a recoverable stray directory, never a dangling DB row.
- Title matching is exact and case-insensitive (`func.lower(title) == text.lower()`), never substring.
- Selector required: no IDs and no `--title` → exit code 2. Selectors given but nothing matched → exit code 1.
- Follow existing CLI patterns in `cli.py` (`get_settings()` → `make_engine` → `make_session_factory` → `session_scope`).

---

### Task 1: `resolve_magazines` resolver in `importer.py`

**Files:**
- Modify: `src/magsearch/importer.py` (add dataclass + function; extend the `from sqlalchemy import select` line to also import `func`)
- Test: `tests/test_importer.py` (add tests alongside existing ones)

**Interfaces:**
- Consumes: `magsearch.models.Magazine`, an active `sqlalchemy.orm.Session`.
- Produces:
  - `ResolvedTargets` dataclass with `found: list[Magazine]` (deduped by `id`, stable order: requested IDs first in given order, then title-only matches ordered by `id`) and `not_found: list[str]` (requested IDs with no row, in given order).
  - `resolve_magazines(session: Session, ids: Sequence[str], title: str | None = None) -> ResolvedTargets`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_importer.py`:

```python
from magsearch.importer import resolve_magazines, ResolvedTargets


def _import(factory, bundle):
    with session_scope(factory) as s:
        import_bundle(bundle, s)


def test_resolve_by_id_found_and_missing(tmp_path, db):
    factory, _ = db
    _import(factory, _ingested_bundle(tmp_path, title="Byte", num_pages=1))
    with session_scope(factory) as s:
        r = resolve_magazines(s, ["byte-1985-12", "nope-1999-01"], None)
        assert [m.id for m in r.found] == ["byte-1985-12"]
        assert r.not_found == ["nope-1999-01"]


def test_resolve_by_title_case_insensitive_multiple(tmp_path, db):
    factory, _ = db
    _import(factory, _ingested_bundle(tmp_path / "a", title="Byte", num_pages=1))
    # Second issue, same title, different date -> different id.
    from datetime import date
    src = make_pdf(tmp_path / "b" / "Byte.pdf", num_pages=1)
    from magsearch.ingest.ocr import FakeOCREngine, OCRRegion
    from magsearch.ingest.pipeline import IngestOptions, IngestPipeline
    pipeline = IngestPipeline(
        bundles_root=tmp_path / "b" / "bundles",
        ocr_engine=FakeOCREngine(responses=[[OCRRegion(text="w", bbox=(0, 0, 5, 5), confidence=1.0)]]),
        options=IngestOptions(title="Byte", publication_date=date(1986, 1, 1)),
    )
    _import(factory, pipeline.run(src).bundle_dir)
    with session_scope(factory) as s:
        r = resolve_magazines(s, [], "byte")
        assert {m.id for m in r.found} == {"byte-1985-12", "byte-1986-01"}
        assert r.not_found == []


def test_resolve_dedupes_id_and_title(tmp_path, db):
    factory, _ = db
    _import(factory, _ingested_bundle(tmp_path, title="Byte", num_pages=1))
    with session_scope(factory) as s:
        r = resolve_magazines(s, ["byte-1985-12"], "Byte")
        assert [m.id for m in r.found] == ["byte-1985-12"]  # not duplicated
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_importer.py -k resolve -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_magazines'`.

- [ ] **Step 3: Implement the resolver**

In `src/magsearch/importer.py`, change the import line:

```python
from sqlalchemy import func, select
```

Add near the top (after the existing imports / `ImportError` class) — note `Sequence`/`dataclass` imports:

```python
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass
class ResolvedTargets:
    found: list[Magazine]
    not_found: list[str]


def resolve_magazines(
    session: Session,
    ids: Sequence[str],
    title: str | None = None,
) -> ResolvedTargets:
    """Resolve IDs + an optional exact title into magazines to delete.

    IDs are looked up in order; unknown IDs go to `not_found`. A title (matched
    case-insensitively, exactly) adds every issue sharing it. The union is
    deduped by id, keeping requested IDs first then title-only matches by id.
    """
    found: list[Magazine] = []
    seen: set[str] = set()
    not_found: list[str] = []
    for mid in ids:
        mag = session.get(Magazine, mid)
        if mag is None:
            not_found.append(mid)
            continue
        if mag.id not in seen:
            seen.add(mag.id)
            found.append(mag)
    if title is not None:
        stmt = (
            select(Magazine)
            .where(func.lower(Magazine.title) == title.lower())
            .order_by(Magazine.id)
        )
        for mag in session.scalars(stmt):
            if mag.id not in seen:
                seen.add(mag.id)
                found.append(mag)
    return ResolvedTargets(found=found, not_found=not_found)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_importer.py -k resolve -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/magsearch/importer.py tests/test_importer.py
git commit -m "feat: resolve_magazines helper for delete-by-id-or-title"
```

---

### Task 2: `delete` CLI command in `cli.py`

**Files:**
- Modify: `src/magsearch/cli.py` (add the command + two private helpers; extend imports)
- Test: `tests/test_cli_delete.py` (new)

**Interfaces:**
- Consumes: `resolve_magazines`, `ResolvedTargets`, `delete_bundle_dir` from `magsearch.importer`; `Magazine` from `magsearch.models`; existing `get_settings`, `make_engine`, `make_session_factory`, `session_scope`.
- Produces: Typer command `delete` registered on `app`. Exit codes: 0 success, 1 no-match, 2 no-selector, 1 on user decline (Typer `Abort`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_delete.py`:

```python
from pathlib import Path

from typer.testing import CliRunner

from magsearch.cli import app

runner = CliRunner()


def _ingest_import(tmp_path, bundles, title, date_str, monkeypatch):
    from tests.fixtures.pdfs import make_pdf
    src = make_pdf(tmp_path / f"{title}-{date_str}.pdf", num_pages=1)
    r = runner.invoke(app, [
        "ingest", str(src), "--title", title, "--date", date_str,
        "--bundles-dir", str(bundles), "--fake-ocr",
    ])
    assert r.exit_code == 0, r.stdout
    # id slug is <title-lower>-<yyyy-mm>
    slug = f"{title.lower()}-{date_str[:7]}"
    r = runner.invoke(app, ["import", str(bundles / slug)])
    assert r.exit_code == 0, r.stdout
    return slug


def _env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    bundles = tmp_path / "bundles"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(bundles))
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    return bundles


def _counts(db_path):
    import sqlite3
    c = sqlite3.connect(db_path)
    try:
        mags = c.execute("SELECT COUNT(*) FROM magazines").fetchone()[0]
        pages = c.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        fts = c.execute("SELECT COUNT(*) FROM pages_fts").fetchone()[0]
        return mags, pages, fts
    finally:
        c.close()


def test_delete_by_id_removes_db_fts_and_files(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01", monkeypatch)
    assert (bundles / slug).exists()
    r = runner.invoke(app, ["delete", slug, "--yes"])
    assert r.exit_code == 0, r.stdout
    assert not (bundles / slug).exists()
    assert _counts(tmp_path / "test.db") == (0, 0, 0)


def test_delete_by_title_removes_all_issues_leaves_others(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    a = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01", monkeypatch)
    b = _ingest_import(tmp_path, bundles, "Byte", "1986-01-01", monkeypatch)
    other = _ingest_import(tmp_path, bundles, "Amiga", "1990-06-01", monkeypatch)
    r = runner.invoke(app, ["delete", "--title", "byte", "--yes"])
    assert r.exit_code == 0, r.stdout
    assert not (bundles / a).exists() and not (bundles / b).exists()
    assert (bundles / other).exists()
    mags, _, _ = _counts(tmp_path / "test.db")
    assert mags == 1


def test_delete_unknown_id_warns_but_deletes_valid(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01", monkeypatch)
    r = runner.invoke(app, ["delete", slug, "does-not-exist", "--yes"])
    assert r.exit_code == 0, r.stdout
    assert "does-not-exist" in r.output
    assert not (bundles / slug).exists()


def test_delete_no_selector_exits_2(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    r = runner.invoke(app, ["delete"])
    assert r.exit_code == 2


def test_delete_no_match_exits_1(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    r = runner.invoke(app, ["delete", "ghost-2000-01"])
    assert r.exit_code == 1


def test_delete_prompt_decline_leaves_data_intact(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01", monkeypatch)
    r = runner.invoke(app, ["delete", slug], input="n\n")
    assert r.exit_code != 0  # aborted
    assert (bundles / slug).exists()
    assert _counts(tmp_path / "test.db")[0] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cli_delete.py -v`
Expected: FAIL — every test errors because the `delete` command doesn't exist (Typer exits 2 with "No such command 'delete'" — the intentional-exit-2 test may spuriously pass, the rest fail; that's fine).

- [ ] **Step 3: Implement the command**

In `src/magsearch/cli.py`, extend the importer import:

```python
from magsearch.importer import (
    ImportError as MagImportError,
    delete_bundle_dir,
    import_bundle,
    resolve_magazines,
)
from magsearch.models import Magazine, User
```

Add the helpers and command (place after the `import` command, near `ocr-rescale`):

```python
def _bundle_dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


@app.command("delete")
def delete_cmd(
    ids: Annotated[list[str] | None, typer.Argument(help="Magazine IDs to delete.")] = None,
    title: Annotated[str | None, typer.Option("--title", help="Also delete every issue with this exact title (case-insensitive).")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete magazines: DB rows, search index, and on-disk bundle files."""
    ids = ids or []
    if not ids and not title:
        typer.echo("delete: specify at least one magazine ID or --title", err=True)
        raise typer.Exit(code=2)

    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)

    deleted_ids: list[str] = []
    total_pages = 0
    total_bytes = 0
    with session_scope(factory) as s:
        targets = resolve_magazines(s, ids, title)
        for missing in targets.not_found:
            typer.echo(f"  ! not found: {missing}", err=True)
        if not targets.found:
            typer.echo("no matching magazines to delete", err=True)
            raise typer.Exit(code=1)

        rows = []
        for mag in targets.found:
            size = _bundle_dir_size(settings.bundles_dir / mag.id)
            total_pages += mag.page_count
            total_bytes += size
            issue = f" №{mag.issue}" if mag.issue else ""
            rows.append(f"  {mag.id} — {mag.title}{issue} ({mag.page_count} pages, {_mb(size)})")

        typer.echo(f"Will delete {len(targets.found)} magazine(s), {total_pages} pages, {_mb(total_bytes)}:")
        for line in rows:
            typer.echo(line)

        if not yes:
            typer.confirm("Proceed?", abort=True)

        deleted_ids = [m.id for m in targets.found]
        for mag in targets.found:
            s.delete(mag)
    # session committed here (DB-first); now remove files.
    for mid in deleted_ids:
        delete_bundle_dir(settings.bundles_dir, mid)

    typer.echo(f"deleted {len(deleted_ids)} magazine(s), {total_pages} pages, freed ~{_mb(total_bytes)}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cli_delete.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `.venv/bin/pytest tests/test_cli.py tests/test_importer.py tests/test_cli_delete.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/magsearch/cli.py tests/test_cli_delete.py
git commit -m "feat: magsearch delete CLI command"
```

---

### Task 3: Document the command in README

**Files:**
- Modify: `README.md` (CLI reference / command list)

- [ ] **Step 1: Find the CLI reference section**

Run: `grep -n "ocr-rescale\|magsearch import\|## CLI\|magsearch web" README.md | head`
Locate where commands are listed (e.g. near line 36 command grid, and the prose sections).

- [ ] **Step 2: Add a `delete` entry**

Add a short subsection near the other command docs, e.g.:

```markdown
### Deleting issues

Remove one or more magazines — database rows, search index, and bundle files —
from the command line (mirrors the admin UI's per-issue delete):

    magsearch delete <magazine-id> [<magazine-id> ...]
    magsearch delete --title "Computer Gaming World"     # all issues of a title
    magsearch delete --title "Computer Gaming World" --yes   # no prompt (scripts)

Prints a summary and asks for confirmation; `--yes`/`-y` skips the prompt for
non-interactive use (e.g. `docker exec magsearch magsearch delete … --yes`).
Deletion is permanent; re-run `magsearch import` on the bundle to restore.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document magsearch delete command"
```

---

## Self-Review Notes

- **Spec coverage:** signature (Task 2), IDs + title union + dedup (Task 1), case-insensitive exact title (Task 1), summary + confirm + `--yes` (Task 2), DB-first-then-disk + cascade + FTS (Task 2, verified by `_counts` asserting `pages_fts` empties), exit codes 2/1/0 and decline (Task 2 tests), README (Task 3). All covered.
- **Types:** `ResolvedTargets.found: list[Magazine]`, `.not_found: list[str]`, `resolve_magazines(session, ids, title=None)` used identically in Task 1 and Task 2.
- **No placeholders:** all steps contain runnable code/commands.
