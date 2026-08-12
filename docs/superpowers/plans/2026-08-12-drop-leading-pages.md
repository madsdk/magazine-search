# `magsearch drop-leading-pages` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `magsearch drop-leading-pages` command that removes leading junk page(s) from already-imported bundles — renumbering pages on disk and updating the database in place — without re-running OCR.

**Architecture:** Repair logic lives in a new Typer-free `src/magsearch/bundle_edit.py` (mirroring `importer.py` / `health.py`), split into three functions: `plan_drop` (validate + describe), `apply_drop` (disk), `resync_magazine` (DB). Disk repair never mutates the live bundle: it hardlinks surviving files into a sibling `<id>.new/` staging directory under their new page numbers and swaps directories atomically. The DB update shifts `page_number` in place so surviving `Page.id`s — and therefore the `pages_fts` index and researchers' saved pages — remain valid.

**Tech Stack:** Python 3.11+, Typer, SQLAlchemy 2.0, Pydantic 2, Pillow, pytest. SQLite with FTS5 external-content index.

## Global Constraints

- Read the design spec first: `docs/superpowers/specs/2026-08-12-drop-leading-pages-design.md`. It is the authority on behavior; this plan is the authority on order and code.
- Ruff config: `line-length = 100`, `target-version = "py311"`, lint rules `["E", "F", "I", "B", "UP", "W"]`. Run `ruff check src tests` before every commit.
- Run tests with `python -m pytest` from `/workspace`. `addopts = "-m 'not ocr'"` already excludes PaddleOCR tests; never add `-m ocr`.
- `src/magsearch/bundle_edit.py` must not import `typer`, and must not import `magsearch.ingest.pipeline`, `magsearch.ingest.formats`, or anything else that pulls in `fitz`/`rarfile` — those live in the optional `[ingest]` extra, and `tests/test_cli_lazy_imports.py` guards against them leaking into the base install.
- `original.<fmt>` is never modified, and `manifest.content_hash`, `manifest.id`, `manifest.original_filename`, `manifest.original_format` are never rewritten.
- Never delete or rewrite a live bundle file in place. All disk repair goes through the staging-directory swap in Task 3.
- Commit after every task with the exact message given in the task's final step.

---

### Task 1: Extract shared checksum helpers

Two private helpers do the same two jobs in two modules today, and the new `bundle_edit` module needs both. Promote them to a public module before anything depends on them.

**Files:**
- Create: `src/magsearch/checksums.py`
- Modify: `src/magsearch/ingest/pipeline.py:189-194` (replace `_collect_checksums` body with a delegating alias), `src/magsearch/importer.py:1-13,81,121-127` (replace `_verify_checksums` body with a delegating alias)
- Test: `tests/test_checksums.py`

**Interfaces:**
- Consumes: `magsearch.manifest.FileChecksum`, `magsearch.manifest.Manifest`, `magsearch.ingest.ids.content_hash`
- Produces:
  - `magsearch.checksums.ChecksumError(Exception)`
  - `magsearch.checksums.collect(bundle_dir: Path) -> list[FileChecksum]`
  - `magsearch.checksums.verify(bundle_dir: Path, manifest: Manifest) -> None` — raises `ChecksumError` on a missing file or hash mismatch
  - `magsearch.ingest.pipeline._collect_checksums` and `magsearch.importer._verify_checksums` keep working with identical signatures and identical raised-exception types

- [ ] **Step 1: Write the failing test**

Create `tests/test_checksums.py`:

```python
from pathlib import Path

import pytest

from magsearch.checksums import ChecksumError, collect, verify
from magsearch.manifest import Manifest
from tests.fixtures.bundles import make_bundle


def _manifest(bundle: Path) -> Manifest:
    return Manifest.model_validate_json((bundle / "manifest.json").read_text())


def test_collect_covers_every_file_except_the_manifest(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)

    paths = {c.path for c in collect(bundle)}

    on_disk = {
        str(p.relative_to(bundle))
        for p in bundle.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    assert paths == on_disk


def test_collect_is_sorted_for_determinism(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)

    paths = [c.path for c in collect(bundle)]

    assert paths == sorted(paths)


def test_verify_accepts_an_untouched_bundle(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)

    verify(bundle, _manifest(bundle))  # must not raise


def test_verify_rejects_a_modified_file(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)
    (bundle / "pages" / "0001.webp").write_bytes(b"corrupted")

    with pytest.raises(ChecksumError, match="checksum mismatch on pages/0001.webp"):
        verify(bundle, _manifest(bundle))


def test_verify_rejects_a_missing_file(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)
    (bundle / "thumbs" / "0002.webp").unlink()

    with pytest.raises(ChecksumError, match="bundle missing file thumbs/0002.webp"):
        verify(bundle, _manifest(bundle))


def test_importer_alias_still_raises_import_error(tmp_path):
    """docs/datamodel/bundles.md tells third-party producers to call this."""
    from magsearch.importer import ImportError as MagImportError, _verify_checksums

    bundle = make_bundle(tmp_path, num_pages=2)
    (bundle / "pages" / "0001.webp").write_bytes(b"corrupted")

    with pytest.raises(MagImportError, match="checksum mismatch"):
        _verify_checksums(bundle, _manifest(bundle))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_checksums.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'magsearch.checksums'` on collection.

- [ ] **Step 3: Write the new module**

Create `src/magsearch/checksums.py`:

```python
from pathlib import Path

from magsearch.ingest.ids import content_hash
from magsearch.manifest import FileChecksum, Manifest

# manifest.json cannot checksum itself, and manifest.json.tmp is the transient
# file the atomic write in pipeline/bundle_edit renames into place.
_EXCLUDED = ("manifest.json", "manifest.json.tmp")


class ChecksumError(Exception):
    """A bundle's files do not match the checksums its manifest declares."""


def collect(bundle_dir: Path) -> list[FileChecksum]:
    """SHA-256 every file under `bundle_dir`, sorted by path for determinism."""
    out: list[FileChecksum] = []
    for p in sorted(bundle_dir.rglob("*")):
        if p.is_file() and p.name not in _EXCLUDED:
            out.append(FileChecksum(path=str(p.relative_to(bundle_dir)), sha256=content_hash(p)))
    return out


def verify(bundle_dir: Path, manifest: Manifest) -> None:
    """Raise ChecksumError unless every file the manifest declares is present and intact."""
    for c in manifest.checksums:
        path = bundle_dir / c.path
        if not path.exists():
            raise ChecksumError(f"bundle missing file {c.path}")
        if content_hash(path) != c.sha256:
            raise ChecksumError(f"checksum mismatch on {c.path}")
```

- [ ] **Step 4: Rewire `importer.py`**

Replace the body of `_verify_checksums` at `src/magsearch/importer.py:121-127` with a delegating alias. `import_bundle` must keep raising `ImportError` (the module's own class), so translate:

```python
def _verify_checksums(bundle_dir: Path, manifest: Manifest) -> None:
    """Kept as the documented entry point for third-party bundle producers
    (docs/datamodel/bundles.md). Delegates to magsearch.checksums.verify and
    re-raises as this module's ImportError so callers' handling is unchanged."""
    try:
        checksums.verify(bundle_dir, manifest)
    except checksums.ChecksumError as exc:
        raise ImportError(str(exc)) from exc
```

Add `from magsearch import checksums` to the imports at the top of `importer.py` and delete the now-unused `from magsearch.ingest.ids import content_hash` line **only if** nothing else in the file uses it (grep first: `grep -n content_hash src/magsearch/importer.py`).

- [ ] **Step 5: Rewire `pipeline.py`**

Replace `_collect_checksums` at `src/magsearch/ingest/pipeline.py:189-194`:

```python
def _collect_checksums(bundle: Path) -> list[FileChecksum]:
    return checksums.collect(bundle)
```

Add `from magsearch import checksums` to the imports. Leave the `FileChecksum` import in place — the annotation still uses it.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest tests/test_checksums.py tests/test_importer.py tests/test_pipeline.py tests/test_health.py -v`
Expected: PASS. `test_importer.py` and `test_pipeline.py` exercise the aliases; a failure there means the delegation changed behavior.

Then: `python -m pytest`
Expected: PASS (whole suite, no regressions).

- [ ] **Step 7: Lint and commit**

```bash
ruff check src tests
git add src/magsearch/checksums.py src/magsearch/importer.py src/magsearch/ingest/pipeline.py tests/test_checksums.py
git commit -m "refactor: extract shared bundle checksum helpers"
```

---

### Task 2: `plan_drop` — validation and the repair plan

The read-only half. Every refusal lives here, so `--dry-run` is "call `plan_drop`, print, stop" and `apply_drop` can assume a valid plan.

**Files:**
- Create: `src/magsearch/bundle_edit.py`
- Test: `tests/test_bundle_edit_plan.py`

**Interfaces:**
- Consumes: `magsearch.checksums.verify`, `magsearch.checksums.ChecksumError`, `magsearch.manifest.Manifest`, `magsearch.manifest.PageEntry`
- Produces:
  - `magsearch.bundle_edit.BundleEditError(Exception)`
  - `magsearch.bundle_edit.DropPlan` — dataclass with fields `bundles_root: Path`, `bundle_dir: Path`, `magazine_id: str`, `count: int`, `manifest: Manifest`, `dropped: list[PageEntry]`, `surviving: list[PageEntry]`, `new_page_count: int`
  - `magsearch.bundle_edit.plan_drop(bundles_root: Path, magazine_id: str, count: int) -> DropPlan`
  - `magsearch.bundle_edit.page_text_preview(entry: PageEntry, width: int = 80) -> str`

`plan_drop` takes the root and the id separately rather than a bundle directory. That is what makes the path-traversal guard possible: the resolved target must be an immediate child of the resolved root, which cannot be checked from a bundle path alone (its own parent is trivially its parent). It also matches how the CLI calls it — `settings.bundles_dir` plus an id — and mirrors `importer.delete_bundle_dir(bundles_root, magazine_id)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bundle_edit_plan.py`:

```python
import json
from pathlib import Path

import pytest

from magsearch.bundle_edit import BundleEditError, page_text_preview, plan_drop
from magsearch.manifest import Manifest, PageEntry
from tests.fixtures.bundles import make_bundle


def _rewrite_manifest(bundle: Path, **updates) -> None:
    """Edit manifest.json in place. Checksums are NOT recomputed — callers that
    need the bundle to still verify must pass consistent data."""
    data = json.loads((bundle / "manifest.json").read_text())
    data.update(updates)
    (bundle / "manifest.json").write_text(json.dumps(data))


def test_plan_splits_dropped_from_surviving(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)

    plan = plan_drop(bundle.parent, bundle.name, count=1)

    assert plan.magazine_id == bundle.name
    assert plan.count == 1
    assert [p.page_number for p in plan.dropped] == [1]
    assert [p.page_number for p in plan.surviving] == [2, 3]
    assert plan.new_page_count == 2


def test_plan_handles_count_above_one(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=4)

    plan = plan_drop(bundle.parent, bundle.name, count=2)

    assert [p.page_number for p in plan.dropped] == [1, 2]
    assert [p.page_number for p in plan.surviving] == [3, 4]
    assert plan.new_page_count == 2


def test_plan_orders_pages_by_number_regardless_of_manifest_order(tmp_path):
    """page_number is the ordering key; the manifest may list pages in any order."""
    bundle = make_bundle(tmp_path, num_pages=3)
    data = json.loads((bundle / "manifest.json").read_text())
    data["pages"] = list(reversed(data["pages"]))
    (bundle / "manifest.json").write_text(json.dumps(data))

    plan = plan_drop(bundle.parent, bundle.name, count=1)

    assert [p.page_number for p in plan.dropped] == [1]
    assert [p.page_number for p in plan.surviving] == [2, 3]


def test_plan_rejects_count_below_one(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)

    with pytest.raises(BundleEditError, match="count must be at least 1"):
        plan_drop(bundle.parent, bundle.name, count=0)


def test_plan_rejects_emptying_the_bundle(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)

    with pytest.raises(BundleEditError, match="would leave 0 pages"):
        plan_drop(bundle.parent, bundle.name, count=2)


def test_plan_rejects_missing_manifest(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)
    (bundle / "manifest.json").unlink()

    with pytest.raises(BundleEditError, match="manifest.json missing"):
        plan_drop(bundle.parent, bundle.name, count=1)


def test_plan_rejects_missing_directory(tmp_path):
    with pytest.raises(BundleEditError, match="manifest.json missing"):
        plan_drop(tmp_path / "bundles", "no-such-bundle", count=1)


def test_plan_rejects_a_traversing_magazine_id(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)

    with pytest.raises(BundleEditError, match="not a bundle directory"):
        plan_drop(bundle.parent, f"../{bundle.parent.name}/{bundle.name}", count=1)


def test_plan_rejects_a_nested_magazine_id(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)

    with pytest.raises(BundleEditError, match="not a bundle directory"):
        plan_drop(bundle.parent.parent, f"bundles/{bundle.name}", count=1)


def test_plan_rejects_an_absolute_magazine_id(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)

    with pytest.raises(BundleEditError, match="not a bundle directory"):
        plan_drop(bundle.parent, str(bundle), count=1)


def test_plan_rejects_unparseable_manifest(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)
    (bundle / "manifest.json").write_text("{not json")

    with pytest.raises(BundleEditError, match="manifest.json unparseable"):
        plan_drop(bundle.parent, bundle.name, count=1)


def test_plan_rejects_non_contiguous_page_numbers(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)
    data = json.loads((bundle / "manifest.json").read_text())
    data["pages"][2]["page_number"] = 9
    (bundle / "manifest.json").write_text(json.dumps(data))

    with pytest.raises(BundleEditError, match="page numbers are not 1..3"):
        plan_drop(bundle.parent, bundle.name, count=1)


def test_plan_rejects_a_bundle_that_already_fails_checksums(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)
    (bundle / "pages" / "0002.webp").write_bytes(b"corrupted")

    with pytest.raises(BundleEditError, match="checksum mismatch on pages/0002.webp"):
        plan_drop(bundle.parent, bundle.name, count=1)


def test_plan_rejects_leftover_staging_directory(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)
    (bundle.parent / f"{bundle.name}.new").mkdir()

    with pytest.raises(BundleEditError, match="left over from an interrupted run"):
        plan_drop(bundle.parent, bundle.name, count=1)


def test_plan_rejects_leftover_old_directory(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)
    (bundle.parent / f"{bundle.name}.old").mkdir()

    with pytest.raises(BundleEditError, match="left over from an interrupted run"):
        plan_drop(bundle.parent, bundle.name, count=1)


def test_preview_truncates_on_a_word_boundary():
    entry = PageEntry(
        page_number=1,
        image_path="pages/0001.webp",
        thumb_path="thumbs/0001.webp",
        ocr_path="ocr/0001.json",
        text="Scanned by RETRO-SCANS in 2019 for the preservation community everywhere online",
    )

    preview = page_text_preview(entry, width=40)

    assert len(preview) <= 41  # 40 chars plus the ellipsis
    assert preview.endswith("…")
    assert not preview.rstrip("…").endswith(" ")
    assert preview.startswith("Scanned by RETRO-SCANS")


def test_preview_marks_empty_text():
    entry = PageEntry(
        page_number=1,
        image_path="pages/0001.webp",
        thumb_path="thumbs/0001.webp",
        ocr_path="ocr/0001.json",
        text="   ",
    )

    assert page_text_preview(entry) == "(no text)"


def test_preview_strips_control_characters():
    entry = PageEntry(
        page_number=1,
        image_path="pages/0001.webp",
        thumb_path="thumbs/0001.webp",
        ocr_path="ocr/0001.json",
        text="line one\nline\ttwo",
    )

    assert page_text_preview(entry) == "line one line two"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_bundle_edit_plan.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'magsearch.bundle_edit'`.

- [ ] **Step 3: Write the implementation**

Create `src/magsearch/bundle_edit.py`:

```python
import re
from dataclasses import dataclass
from pathlib import Path

from magsearch import checksums
from magsearch.manifest import Manifest, PageEntry

STAGING_SUFFIX = ".new"
BACKUP_SUFFIX = ".old"

_WHITESPACE = re.compile(r"\s+")


class BundleEditError(Exception):
    """A bundle cannot be edited safely. The caller should skip it."""


@dataclass
class DropPlan:
    bundles_root: Path
    bundle_dir: Path
    magazine_id: str
    count: int
    manifest: Manifest
    dropped: list[PageEntry]
    surviving: list[PageEntry]
    new_page_count: int


def page_text_preview(entry: PageEntry, width: int = 80) -> str:
    """One-line preview of a page's OCR text, for the operator to eyeball.

    An empty result is meaningful — a logo or credits sheet often OCRs to
    nothing — so it is labelled rather than shown as an empty string.
    """
    text = _WHITESPACE.sub(" ", entry.text).strip()
    if not text:
        return "(no text)"
    if len(text) <= width:
        return text
    return text[:width].rsplit(" ", 1)[0].rstrip() + "…"


def plan_drop(bundles_root: Path, magazine_id: str, count: int) -> DropPlan:
    """Validate a leading-page drop and describe it. Reads only; writes nothing.

    Raises BundleEditError for every condition that makes the repair unsafe, so
    callers can report and skip the bundle without a half-applied edit.
    """
    if count < 1:
        raise BundleEditError(f"count must be at least 1 (got {count})")

    # An id is a single directory name, never a path. Checked before touching
    # the filesystem so a traversing or absolute id can never name a target
    # outside the root — the same guard importer.delete_bundle_dir applies.
    if magazine_id in ("", ".", "..") or magazine_id != Path(magazine_id).name:
        raise BundleEditError(f"refusing to edit {magazine_id!r}: not a bundle directory name")

    root = Path(bundles_root).resolve()
    bundle_dir = (root / magazine_id).resolve()
    if bundle_dir.parent != root:
        raise BundleEditError(f"refusing to edit {bundle_dir}: not a bundle directory")

    for suffix in (STAGING_SUFFIX, BACKUP_SUFFIX):
        stray = bundle_dir.parent / f"{bundle_dir.name}{suffix}"
        if stray.exists():
            raise BundleEditError(
                f"{stray.name} exists — left over from an interrupted run; "
                f"inspect and remove it before retrying"
            )

    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.exists():
        raise BundleEditError(f"manifest.json missing in {bundle_dir}")
    try:
        manifest = Manifest.model_validate_json(manifest_path.read_text())
    except Exception as exc:
        raise BundleEditError(f"manifest.json unparseable: {exc}") from exc

    pages = sorted(manifest.pages, key=lambda p: p.page_number)
    total = len(pages)
    # Renumbering subtracts a constant, so it is only correct when the existing
    # numbering is exactly 1..N with no gaps or duplicates.
    if [p.page_number for p in pages] != list(range(1, total + 1)):
        raise BundleEditError(f"page numbers are not 1..{total} — refusing to renumber")
    if count >= total:
        raise BundleEditError(
            f"dropping {count} of {total} pages would leave 0 pages — "
            f"use `magsearch delete` to remove a magazine"
        )

    # Verify before editing: repairing an already-damaged bundle would write a
    # fresh manifest that certifies the damage as correct.
    try:
        checksums.verify(bundle_dir, manifest)
    except checksums.ChecksumError as exc:
        raise BundleEditError(str(exc)) from exc

    return DropPlan(
        bundles_root=root,
        bundle_dir=bundle_dir,
        magazine_id=manifest.id,
        count=count,
        manifest=manifest,
        dropped=pages[:count],
        surviving=pages[count:],
        new_page_count=total - count,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_bundle_edit_plan.py -v`
Expected: PASS (18 tests).

- [ ] **Step 5: Lint and commit**

```bash
ruff check src tests
git add src/magsearch/bundle_edit.py tests/test_bundle_edit_plan.py
git commit -m "feat: plan_drop validates a leading-page bundle repair"
```

---

### Task 3: `apply_drop` — staged, atomic disk repair

**Files:**
- Modify: `src/magsearch/bundle_edit.py` (append)
- Test: `tests/test_bundle_edit_apply.py`

**Interfaces:**
- Consumes: `DropPlan` from Task 2 (already validated, including the path-traversal guard — `apply_drop` re-checks nothing); `magsearch.checksums.collect`; `magsearch.ingest.normalize.write_cover`
- Produces: `magsearch.bundle_edit.apply_drop(plan: DropPlan) -> Manifest` — returns the rewritten manifest, which Task 4 consumes to update the DB

Background for the implementer: a page's three files are `image_path`, `thumb_path`, `ocr_path`, all bundle-relative (`docs/datamodel/bundles.md`). The reference pipeline names them `pages/0001.webp` etc., but a third-party producer may use any relative path, so renaming must preserve each file's own directory and suffix and only replace the `NNNN` stem. `cover.webp` is a byte copy of the first thumbnail (`normalize.write_cover` is a `shutil.copyfile`), which is exactly why it must be rebuilt rather than hardlinked.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bundle_edit_apply.py`:

```python
import json
from pathlib import Path

import pytest

from magsearch.bundle_edit import apply_drop, plan_drop
from magsearch.checksums import verify
from magsearch.manifest import Manifest
from tests.fixtures.bundles import make_bundle


def _snapshot(bundle: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(bundle)): p.read_bytes()
        for p in sorted(bundle.rglob("*"))
        if p.is_file()
    }


def test_apply_renumbers_pages_and_drops_the_first(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)
    before = _snapshot(bundle)

    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    assert manifest.page_count == 2
    assert [p.page_number for p in manifest.pages] == [1, 2]
    assert [p.image_path for p in manifest.pages] == ["pages/0001.webp", "pages/0002.webp"]
    assert [p.thumb_path for p in manifest.pages] == ["thumbs/0001.webp", "thumbs/0002.webp"]
    assert [p.ocr_path for p in manifest.pages] == ["ocr/0001.json", "ocr/0002.json"]
    # New page 1 is the old page 2, byte for byte.
    assert (bundle / "pages/0001.webp").read_bytes() == before["pages/0002.webp"]
    assert (bundle / "ocr/0001.json").read_bytes() == before["ocr/0002.json"]
    assert not (bundle / "pages/0003.webp").exists()


def test_apply_drops_the_old_page_one_files(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)
    before = _snapshot(bundle)

    apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    remaining = set(_snapshot(bundle).values())
    assert before["pages/0001.webp"] not in remaining
    assert before["ocr/0001.json"] not in remaining


def test_apply_rebuilds_the_cover_from_the_new_first_thumb(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)
    before = _snapshot(bundle)

    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    assert manifest.cover_path == "cover.webp"
    assert (bundle / "cover.webp").read_bytes() == before["thumbs/0002.webp"]
    assert (bundle / "cover.webp").read_bytes() != before["cover.webp"]


def test_apply_writes_a_manifest_that_verifies(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)

    apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    on_disk = Manifest.model_validate_json((bundle / "manifest.json").read_text())
    verify(bundle, on_disk)  # must not raise
    declared = {c.path for c in on_disk.checksums}
    actual = {
        str(p.relative_to(bundle)) for p in bundle.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    assert declared == actual


def test_apply_preserves_provenance_fields(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=3)
    before = Manifest.model_validate_json((bundle / "manifest.json").read_text())

    after = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    assert after.id == before.id
    assert after.content_hash == before.content_hash
    assert after.original_filename == before.original_filename
    assert after.original_format == before.original_format
    assert after.title == before.title
    assert after.publication_date == before.publication_date
    assert after.ocr_engine == before.ocr_engine
    assert (bundle / f"original.{before.original_format}").exists()


def test_apply_with_count_two(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=4)
    before = _snapshot(bundle)

    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=2))

    assert manifest.page_count == 2
    assert [p.page_number for p in manifest.pages] == [1, 2]
    assert (bundle / "pages/0001.webp").read_bytes() == before["pages/0003.webp"]
    assert (bundle / "pages/0002.webp").read_bytes() == before["pages/0004.webp"]


def test_apply_preserves_non_canonical_paths(tmp_path):
    """A third-party producer may use its own naming; only the NNNN stem moves."""
    bundle = make_bundle(tmp_path, num_pages=2)
    (bundle / "img").mkdir()
    for old, new in (("pages/0001.webp", "img/0001.png"), ("pages/0002.webp", "img/0002.png")):
        (bundle / old).rename(bundle / new)
    data = json.loads((bundle / "manifest.json").read_text())
    for entry in data["pages"]:
        entry["image_path"] = f"img/{entry['page_number']:04d}.png"
    data["checksums"] = [
        {**c, "path": c["path"].replace("pages/", "img/").replace(".webp", ".png")}
        if c["path"].startswith("pages/") else c
        for c in data["checksums"]
    ]
    (bundle / "manifest.json").write_text(json.dumps(data))

    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    assert [p.image_path for p in manifest.pages] == ["img/0001.png"]
    assert (bundle / "img/0001.png").exists()
    assert not (bundle / "img/0002.png").exists()


def test_apply_leaves_bundle_untouched_when_the_swap_fails(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, num_pages=3)
    before = _snapshot(bundle)
    plan = plan_drop(bundle.parent, bundle.name, count=1)

    real_rename = Path.rename

    def exploding_rename(self, target):
        # Fail on the first half of the swap: <id> → <id>.old.
        if str(target).endswith(".old"):
            raise OSError("simulated failure during swap")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", exploding_rename)

    with pytest.raises(OSError, match="simulated failure"):
        apply_drop(plan)

    monkeypatch.undo()
    assert _snapshot(bundle) == before
    assert not (bundle.parent / f"{bundle.name}.new").exists()
    assert not (bundle.parent / f"{bundle.name}.old").exists()


def test_apply_restores_the_bundle_when_the_second_rename_fails(tmp_path, monkeypatch):
    """The window between <id> → <id>.old and <id>.new → <id> is the only moment
    the live bundle does not exist. Failing there must put it back."""
    bundle = make_bundle(tmp_path, num_pages=3)
    before = _snapshot(bundle)
    plan = plan_drop(bundle.parent, bundle.name, count=1)

    real_rename = Path.rename

    def exploding_rename(self, target):
        if str(self).endswith(".new"):
            raise OSError("simulated failure restoring")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", exploding_rename)

    with pytest.raises(OSError, match="simulated failure"):
        apply_drop(plan)

    monkeypatch.undo()
    assert _snapshot(bundle) == before
    assert not (bundle.parent / f"{bundle.name}.new").exists()
    assert not (bundle.parent / f"{bundle.name}.old").exists()


def test_apply_is_not_re_runnable_on_an_already_repaired_bundle(tmp_path):
    """Second run drops what is now a real page — it must be an explicit choice,
    never an accident of re-running. It succeeds, so the operator's protection is
    the dry-run preview, not a refusal."""
    bundle = make_bundle(tmp_path, num_pages=3)
    apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    assert manifest.page_count == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_bundle_edit_apply.py -v`
Expected: FAIL — `ImportError: cannot import name 'apply_drop' from 'magsearch.bundle_edit'`.

- [ ] **Step 3: Write the implementation**

Append to `src/magsearch/bundle_edit.py` (and add `import shutil` plus `from magsearch.ingest.normalize import write_cover` to the imports — `normalize` pulls in only Pillow, so this respects the lazy-import constraint):

```python
def _renumbered(rel_path: str, new_number: int) -> str:
    """Rebuild a bundle-relative path with a new NNNN stem, keeping the
    producer's own directory and suffix (paths need not be pages/NNNN.webp)."""
    p = Path(rel_path)
    return str(p.with_name(f"{new_number:04d}{p.suffix}"))


def apply_drop(plan: DropPlan) -> Manifest:
    """Repair the bundle on disk. Returns the manifest now on disk.

    Builds a sibling staging directory of hardlinks and swaps it in, so the live
    bundle is untouched until a single rename, and an interruption anywhere
    leaves it intact.
    """
    # plan_drop already proved bundle_dir is an immediate child of bundles_root.
    bundle = plan.bundle_dir
    root = plan.bundles_root
    staging = root / f"{bundle.name}{STAGING_SUFFIX}"
    backup = root / f"{bundle.name}{BACKUP_SUFFIX}"

    try:
        staging.mkdir()
        new_entries: list[PageEntry] = []
        for new_number, entry in enumerate(plan.surviving, start=1):
            new_entry = PageEntry(
                page_number=new_number,
                image_path=_renumbered(entry.image_path, new_number),
                thumb_path=_renumbered(entry.thumb_path, new_number),
                ocr_path=_renumbered(entry.ocr_path, new_number),
                text=entry.text,
            )
            for old_rel, new_rel in (
                (entry.image_path, new_entry.image_path),
                (entry.thumb_path, new_entry.thumb_path),
                (entry.ocr_path, new_entry.ocr_path),
            ):
                dest = staging / new_rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Hardlink, not copy: staging a 60 MB+ bundle must not cost
                # disk or time, and these files are never written through.
                dest.hardlink_to(bundle / old_rel)
            new_entries.append(new_entry)

        for original in bundle.glob("original.*"):
            (staging / original.name).hardlink_to(original)

        # The old cover is a copy of the junk thumbnail — rebuild, never link.
        cover_rel = ""
        if new_entries:
            write_cover(staging / new_entries[0].thumb_path, staging / "cover.webp")
            cover_rel = "cover.webp"

        manifest = plan.manifest.model_copy(update={
            "page_count": len(new_entries),
            "pages": new_entries,
            "cover_path": cover_rel,
            "checksums": checksums.collect(staging),
        })
        tmp_manifest = staging / "manifest.json.tmp"
        tmp_manifest.write_text(manifest.model_dump_json(indent=2))
        tmp_manifest.replace(staging / "manifest.json")

        bundle.rename(backup)
        try:
            staging.rename(bundle)
        except OSError:
            backup.rename(bundle)  # put the live bundle back before re-raising
            raise
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    shutil.rmtree(backup)
    return manifest
```

Note: use `Path.hardlink_to`, not `Path.link_to` — the latter was removed in Python 3.12, and `mise.toml` pins the toolchain to 3.12. The argument order is `new_link.hardlink_to(existing_file)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_bundle_edit_apply.py -v`
Expected: PASS (10 tests).

If `test_apply_leaves_bundle_untouched_when_the_swap_fails` fails with a leftover `.old` directory, the restore path in the inner `except OSError` is wrong — the outer handler only cleans staging, so the inner one must rename the backup back before re-raising.

- [ ] **Step 5: Lint and commit**

```bash
ruff check src tests
git add src/magsearch/bundle_edit.py tests/test_bundle_edit_apply.py
git commit -m "feat: apply_drop repairs a bundle via a staged directory swap"
```

---

### Task 4: `resync_magazine` — in-place database update

**Files:**
- Modify: `src/magsearch/bundle_edit.py` (append)
- Test: `tests/test_bundle_edit_resync.py`

**Interfaces:**
- Consumes: the `Manifest` returned by `apply_drop`; `magsearch.models.Magazine`, `magsearch.models.Page`
- Produces: `magsearch.bundle_edit.resync_magazine(session: Session, manifest: Manifest, count: int) -> bool` — returns `False` when the magazine has no DB row (nothing done), `True` when rows were updated

Background for the implementer:
- `pages` has `UniqueConstraint("magazine_id", "page_number")` (`models.py:58`), enforced per row, and SQLite gives no ordering guarantee for a bulk `UPDATE`. Shifting straight down can therefore transiently collide with a row that has not been updated yet. The two-pass negation avoids it: no live `page_number` is ever negative.
- `pages_fts` is an external-content FTS5 index keyed on `pages.id`, maintained by the `pages_ai` / `pages_ad` / `pages_au` triggers (`alembic/versions/0001_initial.py:49-80`). Because `Page.id` and `Page.text` never change here, the update trigger's delete+insert is a net no-op and the index stays correct with no explicit work.
- `research_topic_pages.page_id` is `ON DELETE CASCADE` and `PRAGMA foreign_keys=ON` is set on every connection (`db.py:12-16`), so the dropped page's saves clean themselves up.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bundle_edit_resync.py`:

```python
from datetime import date, datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from magsearch.bundle_edit import apply_drop, plan_drop, resync_magazine
from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.importer import import_bundle
from magsearch.models import Magazine, Page, ResearchTopic, ResearchTopicPage, User
from magsearch.web.auth import hash_password
from tests.fixtures.bundles import make_bundle
import pytest


@pytest.fixture
def factory(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
    return make_session_factory(make_engine(f"sqlite:///{db_path}"))


def _imported(tmp_path, factory, num_pages=3) -> Path:
    bundle = make_bundle(tmp_path, num_pages=num_pages)
    with session_scope(factory) as s:
        import_bundle(bundle, s)
    return bundle


def test_resync_shifts_page_numbers_and_paths(tmp_path, factory):
    bundle = _imported(tmp_path, factory)
    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    with session_scope(factory) as s:
        assert resync_magazine(s, manifest, count=1) is True

    with session_scope(factory) as s:
        pages = s.scalars(
            select(Page).where(Page.magazine_id == manifest.id).order_by(Page.page_number)
        ).all()
        assert [p.page_number for p in pages] == [1, 2]
        assert [p.image_path for p in pages] == [
            f"{manifest.id}/pages/0001.webp",
            f"{manifest.id}/pages/0002.webp",
        ]
        assert [p.thumb_path for p in pages] == [
            f"{manifest.id}/thumbs/0001.webp",
            f"{manifest.id}/thumbs/0002.webp",
        ]


def test_resync_preserves_surviving_page_ids(tmp_path, factory):
    bundle = _imported(tmp_path, factory)
    with session_scope(factory) as s:
        ids_before = {
            p.page_number: p.id
            for p in s.scalars(select(Page).order_by(Page.page_number)).all()
        }

    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))
    with session_scope(factory) as s:
        resync_magazine(s, manifest, count=1)

    with session_scope(factory) as s:
        ids_after = {
            p.page_number: p.id
            for p in s.scalars(select(Page).order_by(Page.page_number)).all()
        }
    assert ids_after == {1: ids_before[2], 2: ids_before[3]}


def test_resync_updates_magazine_row(tmp_path, factory):
    bundle = _imported(tmp_path, factory)
    with session_scope(factory) as s:
        ingested_before = s.get(Magazine, bundle.name).ingested_at

    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))
    with session_scope(factory) as s:
        resync_magazine(s, manifest, count=1)

    with session_scope(factory) as s:
        mag = s.get(Magazine, manifest.id)
        assert mag.page_count == 2
        assert mag.cover_path == f"{manifest.id}/cover.webp"
        assert mag.ingested_at == ingested_before  # "Recently filed" must not churn


def test_resync_keeps_research_saves_on_surviving_pages(tmp_path, factory):
    bundle = _imported(tmp_path, factory)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope(factory) as s:
        s.add(User(username="r", password_hash=hash_password("x"),
                   is_researcher=True, created_at=now, last_login_at=None))
        s.flush()
        user_id = s.scalar(select(User.id))
        topic = ResearchTopic(user_id=user_id, name="t", description=None,
                              created_at=now, updated_at=now)
        s.add(topic)
        s.flush()
        page_one = s.scalar(select(Page).where(Page.page_number == 1))
        page_three = s.scalar(select(Page).where(Page.page_number == 3))
        kept_page_id = page_three.id
        s.add(ResearchTopicPage(topic_id=topic.id, page_id=page_one.id,
                                note="junk", saved_at=now))
        s.add(ResearchTopicPage(topic_id=topic.id, page_id=page_three.id,
                                note="keep", saved_at=now))

    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))
    with session_scope(factory) as s:
        resync_magazine(s, manifest, count=1)

    with session_scope(factory) as s:
        saves = s.scalars(select(ResearchTopicPage)).all()
        assert [sv.note for sv in saves] == ["keep"]
        assert saves[0].page_id == kept_page_id
        assert s.get(Page, kept_page_id).page_number == 2


def test_resync_keeps_fts_consistent(tmp_path, factory):
    """make_bundle's FakeOCR writes 'word-N' on page N — unique per page."""
    bundle = _imported(tmp_path, factory)
    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    with session_scope(factory) as s:
        resync_magazine(s, manifest, count=1)

    with session_scope(factory) as s:
        hits = s.execute(text(
            "SELECT pages.page_number FROM pages_fts "
            "JOIN pages ON pages_fts.rowid = pages.id WHERE pages_fts MATCH 'word-3'"
        )).all()
        assert [h[0] for h in hits] == [2]
        gone = s.execute(text(
            "SELECT COUNT(*) FROM pages_fts WHERE pages_fts MATCH 'word-1'"
        )).scalar()
        assert gone == 0
        s.execute(text("INSERT INTO pages_fts(pages_fts) VALUES ('integrity-check')"))


def test_resync_reports_a_missing_magazine_row(tmp_path, factory):
    bundle = make_bundle(tmp_path, num_pages=3)  # never imported
    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=1))

    with session_scope(factory) as s:
        assert resync_magazine(s, manifest, count=1) is False


def test_resync_handles_count_two(tmp_path, factory):
    bundle = _imported(tmp_path, factory, num_pages=4)
    manifest = apply_drop(plan_drop(bundle.parent, bundle.name, count=2))

    with session_scope(factory) as s:
        resync_magazine(s, manifest, count=2)

    with session_scope(factory) as s:
        pages = s.scalars(select(Page).order_by(Page.page_number)).all()
        assert [p.page_number for p in pages] == [1, 2]
        assert s.get(Magazine, manifest.id).page_count == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_bundle_edit_resync.py -v`
Expected: FAIL — `ImportError: cannot import name 'resync_magazine' from 'magsearch.bundle_edit'`.

- [ ] **Step 3: Write the implementation**

Append to `src/magsearch/bundle_edit.py`. Add `from sqlalchemy import select` and `from sqlalchemy.orm import Session` plus `from magsearch.models import Magazine, Page` to the imports:

```python
def resync_magazine(session: Session, manifest: Manifest, count: int) -> bool:
    """Bring the DB rows in line with a bundle whose leading pages were dropped.

    Updates in place rather than delete-and-reimport so surviving Page.ids stay
    valid: research saves keep resolving, the pages_fts external-content index
    stays consistent through the pages_au trigger, and ingested_at is preserved.

    Returns False when the magazine has no row (nothing to do).
    """
    mag = session.get(Magazine, manifest.id)
    if mag is None:
        return False

    dropped = session.scalars(
        select(Page)
        .where(Page.magazine_id == manifest.id, Page.page_number <= count)
    ).all()
    for page in dropped:
        # Per-row ORM DELETE so the pages_ad trigger fires and the FK cascade
        # clears research_topic_pages.
        session.delete(page)
    session.flush()

    survivors = session.scalars(
        select(Page)
        .where(Page.magazine_id == manifest.id)
        .order_by(Page.page_number)
    ).all()

    # Two passes via the negative range. uq_pages_mag_page is checked per row
    # and SQLite gives no ordering guarantee, so a direct `n -= count` can
    # collide with a row that has not shifted yet. No live page_number is
    # negative, so the intermediate values are always free.
    for page in survivors:
        page.page_number = -page.page_number
    session.flush()
    for page in survivors:
        page.page_number = -page.page_number - count
    session.flush()

    by_number = {entry.page_number: entry for entry in manifest.pages}
    for page in survivors:
        entry = by_number[page.page_number]
        page.image_path = f"{manifest.id}/{entry.image_path}"
        page.thumb_path = f"{manifest.id}/{entry.thumb_path}"

    mag.page_count = len(manifest.pages)
    mag.cover_path = f"{manifest.id}/{manifest.cover_path}" if manifest.cover_path else ""
    session.flush()
    return True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_bundle_edit_resync.py -v`
Expected: PASS (7 tests).

If the shift raises `IntegrityError: UNIQUE constraint failed: pages.magazine_id, pages.page_number`, a `session.flush()` between the two passes is missing.

- [ ] **Step 5: Lint and commit**

```bash
ruff check src tests
git add src/magsearch/bundle_edit.py tests/test_bundle_edit_resync.py
git commit -m "feat: resync_magazine shifts page rows in place after a drop"
```

---

### Task 5: The `drop-leading-pages` CLI command

**Files:**
- Modify: `src/magsearch/cli.py` (add the command after `delete_cmd`, which ends at line 324; add `bundle_edit` imports at the top)
- Modify: `README.md` (new section after "Deleting magazines", which ends at line 335)
- Test: `tests/test_cli_drop_leading_pages.py`

**Interfaces:**
- Consumes: `plan_drop`, `apply_drop`, `resync_magazine`, `page_text_preview`, `BundleEditError`, `DropPlan` from Tasks 2–4; `get_settings`, `make_engine`, `make_session_factory`, `session_scope` already imported in `cli.py`
- Produces: the `drop-leading-pages` Typer command. No other module consumes it.

Ordering requirement from the spec, and the reason for it: per bundle, stage → apply DB changes and `flush()` → swap the directory → `commit()`. Because `apply_drop` performs the swap and `session_scope` commits on clean exit, calling `apply_drop` inside the `with session_scope(...)` block after `resync_magazine` gives exactly this order. A DB error rolls back with the bundle untouched; a swap error rolls back the DB too.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_drop_leading_pages.py`:

```python
import json
from pathlib import Path

from typer.testing import CliRunner

from magsearch.cli import app

runner = CliRunner()

# Assertions on message text use `result.output` (stdout + stderr combined)
# rather than `result.stdout`: the command writes refusals with `err=True`, and
# whether those land in `result.stdout` depends on the Click version.


def _env(tmp_path, monkeypatch) -> Path:
    db_path = tmp_path / "test.db"
    bundles = tmp_path / "bundles"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(bundles))
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    return bundles


def _ingest_import(tmp_path, bundles, title, date_str, num_pages=3) -> str:
    from tests.fixtures.pdfs import make_pdf
    src = make_pdf(tmp_path / f"{title}-{date_str}.pdf", num_pages=num_pages)
    r = runner.invoke(app, [
        "ingest", str(src), "--title", title, "--date", date_str,
        "--bundles-dir", str(bundles), "--fake-ocr",
    ])
    assert r.exit_code == 0, r.stdout
    slug = f"{title.lower()}-{date_str[:7]}"
    r = runner.invoke(app, ["import", str(bundles / slug)])
    assert r.exit_code == 0, r.stdout
    return slug


def _page_count(bundles, slug) -> int:
    return json.loads((bundles / slug / "manifest.json").read_text())["page_count"]


def test_help_lists_the_command():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "drop-leading-pages" in result.stdout


def test_dry_run_reports_and_changes_nothing(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")

    r = runner.invoke(app, ["drop-leading-pages", slug, "--dry-run"])

    assert r.exit_code == 0, r.stdout
    assert "drop page 1" in r.output
    assert "3 pages → 2" in r.output
    assert _page_count(bundles, slug) == 3


def test_dry_run_shows_the_page_text(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")

    r = runner.invoke(app, ["drop-leading-pages", slug, "--dry-run"])

    # The fake OCR engine writes the PDF's own page text; whatever it is, the
    # operator must see something to judge by.
    assert "(no text)" in r.output or any(c.isalnum() for c in r.output.split("drop page 1")[1])


def test_drop_repairs_bundle_and_database(tmp_path, monkeypatch):
    import sqlite3

    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")

    r = runner.invoke(app, ["drop-leading-pages", slug, "--yes"])

    assert r.exit_code == 0, r.stdout
    assert _page_count(bundles, slug) == 2
    assert not (bundles / slug / "pages" / "0003.webp").exists()
    c = sqlite3.connect(tmp_path / "test.db")
    try:
        assert c.execute("SELECT page_count FROM magazines").fetchone()[0] == 2
        assert [r[0] for r in c.execute(
            "SELECT page_number FROM pages ORDER BY page_number")] == [1, 2]
        assert c.execute("SELECT COUNT(*) FROM pages_fts").fetchone()[0] == 2
    finally:
        c.close()


def test_repaired_bundle_passes_check(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")

    assert runner.invoke(app, ["drop-leading-pages", slug, "--yes"]).exit_code == 0

    r = runner.invoke(app, ["check", slug, "--checksums"])
    assert r.exit_code == 0, r.stdout


def test_count_flag_drops_two(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01", num_pages=4)

    r = runner.invoke(app, ["drop-leading-pages", slug, "--count", "2", "--yes"])

    assert r.exit_code == 0, r.stdout
    assert _page_count(bundles, slug) == 2


def test_prompt_aborts_without_changes(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")

    r = runner.invoke(app, ["drop-leading-pages", slug], input="n\n")

    assert r.exit_code != 0
    assert _page_count(bundles, slug) == 3


def test_unknown_bundle_is_reported_and_others_still_process(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    good = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")

    r = runner.invoke(app, ["drop-leading-pages", "no-such-bundle", good, "--yes"])

    assert r.exit_code == 1, r.stdout
    assert "no-such-bundle" in r.output
    assert _page_count(bundles, good) == 2


def test_requires_at_least_one_id(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)

    r = runner.invoke(app, ["drop-leading-pages"])

    assert r.exit_code == 2
    assert "specify at least one magazine ID" in r.output


def test_bundle_without_db_row_is_repaired_with_a_warning(tmp_path, monkeypatch):
    from tests.fixtures.pdfs import make_pdf

    bundles = _env(tmp_path, monkeypatch)
    src = make_pdf(tmp_path / "orphan.pdf", num_pages=3)
    r = runner.invoke(app, [
        "ingest", str(src), "--title", "Orphan", "--date", "1985-12-01",
        "--bundles-dir", str(bundles), "--fake-ocr",
    ])
    assert r.exit_code == 0, r.stdout

    r = runner.invoke(app, ["drop-leading-pages", "orphan-1985-12", "--yes"])

    assert r.exit_code == 0, r.stdout
    assert "not in database" in r.output
    assert _page_count(bundles, "orphan-1985-12") == 2


def test_refusal_leaves_the_bundle_intact(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    (bundles / slug / "pages" / "0002.webp").write_bytes(b"corrupted")

    r = runner.invoke(app, ["drop-leading-pages", slug, "--yes"])

    assert r.exit_code == 1
    assert "checksum mismatch" in r.output
    assert _page_count(bundles, slug) == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_cli_drop_leading_pages.py -v`
Expected: FAIL — every test errors with `No such command 'drop-leading-pages'` (exit code 2).

- [ ] **Step 3: Write the command**

Add to `src/magsearch/cli.py` after `delete_cmd` (which ends at line 324). Add to the imports at the top:

```python
from magsearch.bundle_edit import (
    BundleEditError,
    apply_drop,
    page_text_preview,
    plan_drop,
    resync_magazine,
)
```

```python
@app.command("drop-leading-pages")
def drop_leading_pages_cmd(
    ids: Annotated[list[str] | None, typer.Argument(help="Magazine IDs to repair.")] = None,
    count: Annotated[
        int,
        typer.Option("--count", help="Number of leading pages to drop from each bundle."),
    ] = 1,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Report what would be dropped and stop."),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Drop junk leading page(s) from imported bundles and renumber the rest.

    For issues whose archive put a scan-credits or logo sheet ahead of the
    cover. Renumbers pages on disk and updates the database in place — no
    re-OCR, and `original.<ext>` is left byte-identical.
    """
    ids = ids or []
    if not ids:
        typer.echo("drop-leading-pages: specify at least one magazine ID", err=True)
        raise typer.Exit(code=2)

    settings = get_settings()

    plans = []
    failed = 0
    for mid in ids:
        try:
            plan = plan_drop(settings.bundles_dir, mid, count)
        except BundleEditError as exc:
            typer.echo(f"  ! {mid}: {exc}", err=True)
            failed += 1
            continue
        plans.append(plan)

    for plan in plans:
        total = len(plan.manifest.pages)
        typer.echo(f"{plan.magazine_id} — {plan.manifest.title} ({total} pages → {plan.new_page_count})")
        for entry in plan.dropped:
            typer.echo(
                f"  drop page {entry.page_number}  {entry.image_path}  "
                f"\"{page_text_preview(entry)}\""
            )
        first = plan.surviving[0]
        typer.echo(
            f"  new page 1 ← old page {first.page_number}  "
            f"{first.thumb_path} becomes the cover"
        )

    if dry_run:
        typer.echo(f"dry run: {len(plans)} bundle(s) would be repaired, {failed} skipped")
        raise typer.Exit(code=1 if failed else 0)

    if not plans:
        typer.echo("no bundles to repair", err=True)
        raise typer.Exit(code=1)

    if not yes:
        typer.confirm("Proceed?", abort=True)

    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    repaired = 0
    for plan in plans:
        try:
            # session_scope commits on clean exit, so putting apply_drop last
            # inside the block gives: DB flush → directory swap → commit. A DB
            # error rolls back with the bundle untouched; a swap error rolls
            # back the DB too.
            with session_scope(factory) as s:
                manifest = apply_drop(plan)
                if not resync_magazine(s, manifest, plan.count):
                    typer.echo(
                        f"  ! {plan.magazine_id}: repaired on disk but not in database — "
                        f"run `magsearch import {settings.bundles_dir / plan.magazine_id}`"
                    )
        except (BundleEditError, OSError, DatabaseError) as exc:
            typer.echo(f"  ! {plan.magazine_id}: {exc}", err=True)
            failed += 1
            continue
        typer.echo(f"  ✓ {plan.magazine_id}: {plan.new_page_count} pages")
        repaired += 1

    typer.echo(f"drop-leading-pages: {repaired} bundle(s) repaired, {failed} skipped")
    if failed:
        raise typer.Exit(code=1)
```

Note: `apply_drop` runs before `resync_magazine` in source order but the DB work is flushed inside the same block — SQLAlchemy defers the commit to `session_scope`'s exit, which is what puts the commit after the swap.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_cli_drop_leading_pages.py -v`
Expected: PASS (11 tests).

- [ ] **Step 5: Confirm the lazy-import guard still holds**

Run: `python -m pytest tests/test_cli_lazy_imports.py -v`
Expected: PASS. `bundle_edit` is imported eagerly at the top of `cli.py`, so it must not transitively import `fitz` or `rarfile`. A failure here means an import crept into `bundle_edit.py` — move it inside the function or drop it.

- [ ] **Step 6: Document the command in the README**

Add after the "Deleting magazines" section (ends at line 335), before "## Checking bundle health":

```markdown
## Fixing a junk first page

Some CBR archives put a scan-credits sheet or release-group logo ahead of the
cover. It becomes page 1, the real cover becomes page 2, and the whole issue is
off by one.

```
magsearch drop-leading-pages <magazine-id> --dry-run   # confirm it's junk
magsearch drop-leading-pages <magazine-id>
magsearch drop-leading-pages <id-a> <id-b> --count 2 --yes
```

The dry run prints each page it would drop together with that page's OCR text,
which is how you tell a credits sheet from a cover. The repair renumbers the
remaining pages on disk, rebuilds `cover.webp`, rewrites the manifest, and
shifts the database rows in place. OCR is not re-run, and `original.<ext>` is
left byte-identical — so the archive you downloaded still contains the junk
image, and bundle page N now corresponds to archive page N+1.

`--count` applies to every ID in one invocation. Verify afterwards with
`magsearch check --checksums <magazine-id>`.
```

- [ ] **Step 7: Run the full suite, lint, and commit**

```bash
python -m pytest
ruff check src tests
git add src/magsearch/cli.py tests/test_cli_drop_leading_pages.py README.md
git commit -m "feat: add magsearch drop-leading-pages command"
```

---

### Task 6: Guard `ocr-rescale` against page-count drift

`ocr_rescale_cmd` pairs `read_pages(original, fmt)` output with `NNNN` stems positionally (`cli.py:554-558`). On a repaired bundle, bundle page N is archive page N+count, so it would rescale each page's bboxes against the wrong source dimensions — silently, since the scale factors remain plausible numbers.

**Files:**
- Modify: `src/magsearch/cli.py:517-530` (add the pre-flight inside the per-bundle loop, after `detect_format` succeeds)
- Modify: `README.md:355-361` (the ocr-rescale note under "Checking bundle health")
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `magsearch.ingest.formats.page_count` (already imported lazily inside `ocr_rescale_cmd` alongside `detect_format`), `magsearch.manifest.Manifest`
- Produces: nothing new. Behavior change only.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py`:

```python
def test_ocr_rescale_skips_a_bundle_whose_page_count_drifted(tmp_path, monkeypatch):
    """A repaired bundle has fewer pages than its archive; pairing them
    positionally would rescale each page against the wrong source image."""
    import json

    from tests.fixtures.bundles import make_bundle

    bundle = make_bundle(tmp_path, num_pages=3)
    # Put the OCR JSONs back in the old array format so there is work to skip.
    for ocr_path in (bundle / "ocr").glob("*.json"):
        doc = json.loads(ocr_path.read_text())
        ocr_path.write_text(json.dumps(doc["regions"]))
    # Simulate a repaired bundle: manifest says 2 pages, original.pdf has 3.
    data = json.loads((bundle / "manifest.json").read_text())
    data["page_count"] = 2
    data["pages"] = data["pages"][:2]
    (bundle / "manifest.json").write_text(json.dumps(data))

    r = runner.invoke(app, ["ocr-rescale", str(bundle.parent)])

    assert r.exit_code == 0, r.stdout
    assert "does not match the archive" in r.output
    # Untouched: still the old array format.
    assert isinstance(json.loads((bundle / "ocr" / "0001.json").read_text()), list)


def test_ocr_rescale_still_processes_a_consistent_bundle(tmp_path, monkeypatch):
    import json

    from tests.fixtures.bundles import make_bundle

    bundle = make_bundle(tmp_path, num_pages=2)
    for ocr_path in (bundle / "ocr").glob("*.json"):
        doc = json.loads(ocr_path.read_text())
        ocr_path.write_text(json.dumps(doc["regions"]))

    r = runner.invoke(app, ["ocr-rescale", str(bundle.parent)])

    assert r.exit_code == 0, r.stdout
    assert isinstance(json.loads((bundle / "ocr" / "0001.json").read_text()), dict)
```

- [ ] **Step 2: Run the tests to verify the first fails**

Run: `python -m pytest tests/test_cli.py -k ocr_rescale -v`
Expected: `test_ocr_rescale_skips_a_bundle_whose_page_count_drifted` FAILS (`"does not match the archive" not in stdout` — the bundle gets rescaled instead of skipped). `test_ocr_rescale_still_processes_a_consistent_bundle` PASSES already; it is the regression guard for Step 3.

- [ ] **Step 3: Add the pre-flight**

In `ocr_rescale_cmd`, change the lazy import line from

```python
    from magsearch.ingest.formats import detect_format, read_pages
```

to

```python
    from magsearch.ingest.formats import detect_format, page_count, read_pages
```

and insert this immediately after the `detect_format` try/except block (currently ending at `cli.py:529`), before the "Pre-flight: how many OCR JSONs..." comment:

```python
        # Positional pairing only holds while bundle page N is archive page N.
        # `drop-leading-pages` breaks that on purpose, and a rescale against the
        # wrong source image's dimensions would be silently plausible.
        try:
            manifest = Manifest.model_validate_json((bundle / "manifest.json").read_text())
            archive_pages = page_count(original, fmt)
        except Exception as exc:
            typer.echo(f"  ! {bundle.name}: cannot read page counts: {exc} — skipping", err=True)
            failed_bundles += 1
            continue
        if manifest.page_count != archive_pages:
            typer.echo(
                f"  · {bundle.name}: manifest has {manifest.page_count} pages but "
                f"{original.name} has {archive_pages} — page numbering does not match "
                f"the archive (dropped pages?); skipping"
            )
            skipped_bundles += 1
            continue
```

Add `from magsearch.manifest import Manifest` to the imports at the top of `cli.py` — `manifest.py` depends only on Pydantic, so it is safe to import eagerly.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_cli.py -k ocr_rescale -v`
Expected: PASS (both).

- [ ] **Step 5: Update the README note**

In the paragraph at `README.md:355-361` that recommends `magsearch ocr-rescale`, append:

```markdown
`ocr-rescale` skips any bundle whose `manifest.page_count` differs from its
`original.<ext>` page count — for example after `magsearch drop-leading-pages` —
because it pairs bundle pages with archive pages by position, and a mismatched
pairing would rescale bboxes against the wrong source image.
```

- [ ] **Step 6: Run the full suite, lint, and commit**

```bash
python -m pytest
ruff check src tests
git add src/magsearch/cli.py tests/test_cli.py README.md
git commit -m "fix: skip page-count-drifted bundles in ocr-rescale"
```

---

### Task 7: Document the repair in the bundle format spec

`docs/datamodel/bundles.md` is the contract third-party bundle producers read. It currently states that page numbering comes from the archive and that there is no way to change an imported bundle — both now have an exception.

**Files:**
- Modify: `docs/datamodel/bundles.md` (append a section after "Re-import behavior", which ends at line 242)

**Interfaces:**
- Consumes: nothing. Documentation only.
- Produces: nothing.

- [ ] **Step 1: Add the section**

Insert after the "Re-import behavior" section and before "## What ends up in the database vs. on disk":

```markdown
## Repairing an imported bundle

`import_bundle` has no force-replace path, but a bundle whose leading pages are
junk (a scan-credits sheet ahead of the cover, common in CBR archives) can be
repaired in place with `magsearch drop-leading-pages`. The command is the only
supported mutation of an imported bundle. It:

- deletes the dropped pages' `image_path`, `thumb_path` and `ocr_path` files,
- renumbers every surviving page down by the drop count, rewriting each file's
  `NNNN` stem while keeping its directory and suffix,
- rebuilds `cover.webp` from the new first thumbnail,
- rewrites `page_count`, `pages` and `checksums`,
- leaves `id`, `content_hash`, `original_filename`, `original_format` and
  `original.<fmt>` untouched.

It refuses to touch a bundle whose checksums do not already verify, whose page
numbers are not exactly `1..N`, or where the drop would leave zero pages.

**Consequence for producers and tools:** after a repair, bundle page `N` is
archive page `N + count`. Any tool that pairs a bundle's pages with its
`original.<fmt>` positionally must first check `manifest.page_count` against the
archive's own page count and refuse when they differ — `magsearch ocr-rescale`
does exactly this.
```

- [ ] **Step 2: Verify the referenced behavior matches the implementation**

Run: `python -m pytest tests/test_bundle_edit_apply.py tests/test_bundle_edit_plan.py -v`
Expected: PASS. Each claim in the new section corresponds to a test there — if one fails, the doc is describing behavior that does not exist.

- [ ] **Step 3: Commit**

```bash
git add docs/datamodel/bundles.md
git commit -m "docs: document bundle repair in the bundle format spec"
```

---

## Final verification

- [ ] Run the whole suite: `python -m pytest` — expect all green.
- [ ] Lint: `ruff check src tests` — expect no findings.
- [ ] Exercise it end to end against a scratch copy of real data before touching production:

```bash
cp -r data/bundles/<some-id> /tmp/repair-test/bundles/
MAGSEARCH_BUNDLES_DIR=/tmp/repair-test/bundles \
MAGSEARCH_DATABASE_URL=sqlite:////tmp/repair-test/test.db \
  magsearch drop-leading-pages <some-id> --dry-run
```

- [ ] On the deployment, for each affected issue: `--dry-run` first and read the page text, then repair, then `magsearch check --checksums <id>`, then confirm the cover in the web UI.
