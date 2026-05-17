# Web bundle upload — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an admin-only web page at `/admin/issues/upload` that accepts a `.zip` of a bundle directory, validates it, stages it under `bundles_dir/`, and imports it into the DB — collapsing the rsync+ssh+import dance into one click for single-issue uploads.

**Architecture:** Pure validation/staging logic in `src/magsearch/web/bundle_upload.py` (`extract_and_stage`), invoked by a thin FastAPI route in `routes_admin.py` that then calls the existing `import_bundle()`. Synchronous request handling, vanilla-JS upload progress, atomic on success and residue-free on failure.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, pydantic, stdlib `zipfile` / `hashlib` / `shutil`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-05-17-web-bundle-upload-design.md`.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/magsearch/settings.py` | Modify | Add `max_upload_bytes` (default 2 GB) |
| `src/magsearch/web/bundle_upload.py` | Create | `BundleUploadError`, `extract_and_stage()` |
| `src/magsearch/web/routes_admin.py` | Modify | GET + POST `/admin/issues/upload` |
| `src/magsearch/web/templates/admin/issue_upload.html` | Create | Upload form + progress JS |
| `src/magsearch/web/templates/admin/issues.html` | Modify | "Upload bundle" link |
| `tests/test_web_bundle_upload.py` | Create | Pure-logic + route tests |
| `tests/fixtures/bundles.py` | Create | `make_bundle_zip()` test helper |
| `README.md` | Modify | New section + env-var row + auth note |

**Note on `import_bundle()` signature.** The actual signature is `import_bundle(bundle_dir: Path, session: Session) -> str` (see `src/magsearch/importer.py:36`). The spec's mention of a third `bundles_dir` argument was a mistake — the plan below uses the real signature.

**Note on success redirect target.** The spec said "redirect to /admin/issues/<id>" but there is no detail view at that path today — only `/admin/issues/<id>/edit`. The plan redirects to `/admin/issues/<id>/edit` (the editor), which lets the admin immediately tweak metadata on the freshly imported issue.

---

## Task 1: Settings — add `max_upload_bytes`

**Files:**
- Modify: `src/magsearch/settings.py`
- Test: `tests/test_settings.py`

- [ ] **Step 1: Read the current test file**

Run: `cat tests/test_settings.py`
Note the existing structure so the new test follows the same patterns.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_settings.py`:

```python
def test_settings_max_upload_bytes_default(monkeypatch):
    # Reset overrides so the default takes effect.
    monkeypatch.delenv("MAGSEARCH_MAX_UPLOAD_BYTES", raising=False)
    from magsearch.settings import Settings
    s = Settings()
    assert s.max_upload_bytes == 2 * 1024 * 1024 * 1024  # 2 GB


def test_settings_max_upload_bytes_override(monkeypatch):
    monkeypatch.setenv("MAGSEARCH_MAX_UPLOAD_BYTES", "1048576")  # 1 MB
    from magsearch.settings import Settings
    s = Settings()
    assert s.max_upload_bytes == 1048576
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `pytest tests/test_settings.py::test_settings_max_upload_bytes_default tests/test_settings.py::test_settings_max_upload_bytes_override -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'max_upload_bytes'`.

- [ ] **Step 4: Implement**

Edit `src/magsearch/settings.py` — add the new field alongside the existing ones:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MAGSEARCH_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/magsearch.db"
    bundles_dir: Path = Path("./data/bundles")
    session_secret: str = ""
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GB cap on web bundle uploads
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `pytest tests/test_settings.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/magsearch/settings.py tests/test_settings.py
git commit -m "Add max_upload_bytes setting for web bundle uploads."
```

---

## Task 2: Bundle-zip test fixture helper

A reusable helper to build a real bundle (using `FakeOCREngine`) and zip it. Lives next to the other fixtures.

**Files:**
- Create: `tests/fixtures/bundles.py`

- [ ] **Step 1: Check existing fixtures**

Run: `ls tests/fixtures/`
Confirm `pdfs.py` exists (used by other tests for `make_pdf`).

- [ ] **Step 2: Create the helper**

Create `tests/fixtures/bundles.py`:

```python
"""Test helpers that build real bundles using FakeOCREngine, then optionally
zip them in either of the two layouts the web upload accepts."""

from __future__ import annotations

import zipfile
from datetime import date
from pathlib import Path

from magsearch.ingest.ocr import FakeOCREngine, OCRRegion
from magsearch.ingest.pipeline import IngestOptions, IngestPipeline
from tests.fixtures.pdfs import make_pdf


def make_bundle(
    workdir: Path,
    *,
    title: str = "Byte",
    num_pages: int = 2,
    publication_date: date | None = None,
) -> Path:
    """Build a real bundle directory under workdir/bundles/<id>. Returns the dir."""
    workdir.mkdir(parents=True, exist_ok=True)
    src = make_pdf(workdir / f"{title}.pdf", num_pages=num_pages)
    bundles = workdir / "bundles"
    pipeline = IngestPipeline(
        bundles_root=bundles,
        ocr_engine=FakeOCREngine(responses=[
            [OCRRegion(text=f"word-{i+1}", bbox=(0, 0, 50, 10), confidence=1.0)]
            for i in range(num_pages)
        ]),
        options=IngestOptions(
            title=title,
            publication_date=publication_date or date(1985, 12, 1),
        ),
    )
    return pipeline.run(src).bundle_dir


def zip_bundle(bundle_dir: Path, zip_path: Path, *, shape: str = "A") -> Path:
    """Zip a bundle directory.

    Shape A (default): a single top-level directory (the bundle id) contains
    everything. Matches `cd bundles && zip -r out.zip <id>/`.

    Shape B: bundle contents are at the zip root, no enclosing directory.
    Matches `cd <bundle-id> && zip -r ../out.zip .`.
    """
    if shape not in {"A", "B"}:
        raise ValueError(f"shape must be 'A' or 'B', got {shape!r}")
    bundle_dir = bundle_dir.resolve()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(bundle_dir.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(bundle_dir)
            arcname = f"{bundle_dir.name}/{rel.as_posix()}" if shape == "A" else rel.as_posix()
            zf.write(path, arcname=arcname)
    return zip_path
```

- [ ] **Step 3: Smoke-test the helper interactively**

Run: `python -c "from tests.fixtures.bundles import make_bundle, zip_bundle; from pathlib import Path; import tempfile, os; d = Path(tempfile.mkdtemp()); b = make_bundle(d); z = zip_bundle(b, d / 'test.zip'); print('OK', z, z.stat().st_size); import shutil; shutil.rmtree(d)"`
Expected: prints `OK <path> <size>` with size > 0. No exceptions.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/bundles.py
git commit -m "Add bundle-zip test fixture helper."
```

---

## Task 3: `bundle_upload.py` — happy path Shape A

Build the skeleton module and make Shape-A extraction work end-to-end. Subsequent tasks will add rejection paths.

**Files:**
- Create: `src/magsearch/web/bundle_upload.py`
- Create: `tests/test_web_bundle_upload.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_web_bundle_upload.py`:

```python
"""Tests for src/magsearch/web/bundle_upload.py.

Pure-logic tests (this file) drive `extract_and_stage` directly with hand-rolled
zip files. Route-level tests come later via FastAPI's TestClient.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from magsearch.web.bundle_upload import BundleUploadError, extract_and_stage
from tests.fixtures.bundles import make_bundle, zip_bundle


_2_GB = 2 * 1024 * 1024 * 1024


def test_extract_and_stage_shape_a_publishes_bundle(tmp_path):
    src = make_bundle(tmp_path / "src")
    bundle_id = src.name
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    final_root = tmp_path / "final"
    final_root.mkdir()

    staged = extract_and_stage(
        zip_path, final_root, max_uncompressed_bytes=_2_GB,
    )

    assert staged == final_root / bundle_id
    assert (staged / "manifest.json").exists()
    # original.* and the page assets came through:
    assert any(staged.glob("original.*"))
    assert (staged / "pages").is_dir()
    assert (staged / "thumbs").is_dir()
    # No residue temp dir left behind.
    leftovers = [p.name for p in final_root.iterdir() if p.name.startswith(".upload-")]
    assert leftovers == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_web_bundle_upload.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'magsearch.web.bundle_upload'`.

- [ ] **Step 3: Write the minimal implementation**

Create `src/magsearch/web/bundle_upload.py`:

```python
"""Pure validation/staging logic for the web bundle upload endpoint.

`extract_and_stage` is the single entry point. It validates a zipped bundle,
extracts it to a temp staging directory under `bundles_dir`, verifies the
manifest's per-file checksums, and atomic-renames the staging directory into
its final location `bundles_dir/<id>/`. It does NOT touch the database; the
caller invokes `import_bundle()` after staging succeeds.

Invariants:
  - Atomic on success: the bundle either appears fully at bundles_dir/<id>/
    or not at all.
  - No residue on failure: temp staging directory is removed.
  - Idempotent: re-uploading the same content returns the existing path
    without re-extracting.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
import zipfile
from pathlib import Path

from magsearch.ingest.ids import content_hash
from magsearch.manifest import Manifest


class BundleUploadError(Exception):
    """Raised when an uploaded zip is rejected. Message is safe to display."""


def extract_and_stage(
    zip_path: Path,
    bundles_dir: Path,
    *,
    max_uncompressed_bytes: int,
) -> Path:
    """Validate `zip_path` and stage it under `bundles_dir/<id>/`.

    Returns the staged bundle directory.
    """
    bundles_dir = Path(bundles_dir)
    bundles_dir.mkdir(parents=True, exist_ok=True)

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise BundleUploadError("file is not a valid zip archive")

    with zf:
        prefix = _resolve_bundle_prefix(zf)
        manifest = _read_manifest(zf, prefix)

        staging = bundles_dir / f".upload-{manifest.id}-{uuid.uuid4().hex}"
        try:
            _extract_under_prefix(zf, prefix, staging)
            _verify_checksums(staging, manifest)
            final = bundles_dir / manifest.id
            os.rename(staging, final)
            return final
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise


def _resolve_bundle_prefix(zf: zipfile.ZipFile) -> str:
    """Locate the in-zip path that holds manifest.json.

    Returns either "" (manifest at root) or "<dir>/" (single top-level folder).
    Raises BundleUploadError otherwise.
    """
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if "manifest.json" in names:
        return ""
    top_dirs = {n.split("/", 1)[0] for n in names if "/" in n}
    if len(top_dirs) == 1:
        only = next(iter(top_dirs))
        if f"{only}/manifest.json" in names:
            return f"{only}/"
    raise BundleUploadError(
        "manifest.json not found at zip root or in a single top-level folder"
    )


def _read_manifest(zf: zipfile.ZipFile, prefix: str) -> Manifest:
    try:
        raw = zf.read(f"{prefix}manifest.json")
    except KeyError:
        raise BundleUploadError("manifest.json missing from zip")
    try:
        return Manifest.model_validate_json(raw)
    except Exception as exc:  # pydantic ValidationError or json error
        raise BundleUploadError(f"manifest.json is invalid: {exc}")


def _extract_under_prefix(zf: zipfile.ZipFile, prefix: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=False)
    dest_resolved = dest.resolve()
    for info in zf.infolist():
        name = info.filename
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if not rel or rel.endswith("/"):
            continue
        target = (dest / rel).resolve()
        try:
            target.relative_to(dest_resolved)
        except ValueError:
            raise BundleUploadError(f"zip contains unsafe path: {name!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, target.open("wb") as out:
            shutil.copyfileobj(src, out)


def _verify_checksums(bundle_dir: Path, manifest: Manifest) -> None:
    for c in manifest.checksums:
        path = bundle_dir / c.path
        if not path.exists():
            raise BundleUploadError(f"file listed in manifest is missing: {c.path}")
        if content_hash(path) != c.sha256:
            raise BundleUploadError(f"checksum mismatch: {c.path}")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_web_bundle_upload.py::test_extract_and_stage_shape_a_publishes_bundle -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/magsearch/web/bundle_upload.py tests/test_web_bundle_upload.py
git commit -m "Add bundle_upload module with Shape A extraction happy path."
```

---

## Task 4: `bundle_upload.py` — Shape B tolerance

Accept zips whose contents are at the root instead of inside a top-level folder.

**Files:**
- Modify: `tests/test_web_bundle_upload.py`

- [ ] **Step 1: Add the test**

Append to `tests/test_web_bundle_upload.py`:

```python
def test_extract_and_stage_shape_b_publishes_bundle(tmp_path):
    src = make_bundle(tmp_path / "src")
    bundle_id = src.name
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="B")

    final_root = tmp_path / "final"
    final_root.mkdir()

    staged = extract_and_stage(
        zip_path, final_root, max_uncompressed_bytes=_2_GB,
    )

    assert staged == final_root / bundle_id
    assert (staged / "manifest.json").exists()
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_web_bundle_upload.py::test_extract_and_stage_shape_b_publishes_bundle -v`
Expected: PASS. (Shape B is already handled by `_resolve_bundle_prefix` returning `""`.)

If it fails, fix `_resolve_bundle_prefix` and re-run.

- [ ] **Step 3: Commit**

```bash
git add tests/test_web_bundle_upload.py
git commit -m "Verify Shape B (no top-level dir) bundle zips are accepted."
```

---

## Task 5: `bundle_upload.py` — pre-extraction rejections

A single task covering the rejections that bail before any extraction: corrupt zip, manifest missing, manifest invalid, size limit, path traversal.

**Files:**
- Modify: `src/magsearch/web/bundle_upload.py`
- Modify: `tests/test_web_bundle_upload.py`

- [ ] **Step 1: Add tests for each pre-extraction rejection**

Append to `tests/test_web_bundle_upload.py`:

```python
def test_corrupt_zip_is_rejected(tmp_path):
    bad = tmp_path / "not-a-zip.zip"
    bad.write_bytes(b"this is not a zip file")
    final_root = tmp_path / "final"
    final_root.mkdir()
    with pytest.raises(BundleUploadError, match="not a valid zip"):
        extract_and_stage(bad, final_root, max_uncompressed_bytes=_2_GB)
    assert list(final_root.iterdir()) == []


def test_manifest_missing_is_rejected(tmp_path):
    z = tmp_path / "no-manifest.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("something/else.txt", "hi")
    final_root = tmp_path / "final"
    final_root.mkdir()
    with pytest.raises(BundleUploadError, match="manifest.json not found"):
        extract_and_stage(z, final_root, max_uncompressed_bytes=_2_GB)
    assert list(final_root.iterdir()) == []


def test_invalid_manifest_is_rejected(tmp_path):
    z = tmp_path / "bad-manifest.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("manifest.json", "{not valid json")
    final_root = tmp_path / "final"
    final_root.mkdir()
    with pytest.raises(BundleUploadError, match="manifest.json is invalid"):
        extract_and_stage(z, final_root, max_uncompressed_bytes=_2_GB)
    assert list(final_root.iterdir()) == []


def test_size_limit_is_enforced(tmp_path):
    src = make_bundle(tmp_path / "src")
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")
    final_root = tmp_path / "final"
    final_root.mkdir()
    with pytest.raises(BundleUploadError, match="exceed max size"):
        extract_and_stage(zip_path, final_root, max_uncompressed_bytes=10)  # absurdly small
    assert list(final_root.iterdir()) == []


def test_path_traversal_is_rejected(tmp_path):
    # Build a valid bundle to grab a real manifest from, then add a malicious entry.
    src = make_bundle(tmp_path / "src")
    real_manifest = (src / "manifest.json").read_bytes()
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("manifest.json", real_manifest)
        zf.writestr("../escape.txt", "pwned")
    final_root = tmp_path / "final"
    final_root.mkdir()
    with pytest.raises(BundleUploadError, match="unsafe path"):
        extract_and_stage(z, final_root, max_uncompressed_bytes=_2_GB)
    assert list(final_root.iterdir()) == []
```

- [ ] **Step 2: Run the tests to see which fail**

Run: `pytest tests/test_web_bundle_upload.py -v`

Expected: the corrupt-zip, manifest-missing, and invalid-manifest tests pass (already handled by Task 3). The size-limit and path-traversal tests FAIL (size check not yet implemented; traversal check is inside `_extract_under_prefix`, but the size check runs before extraction).

- [ ] **Step 3: Add the size-limit check**

Edit `src/magsearch/web/bundle_upload.py`. Inside `extract_and_stage`, after parsing the manifest and before creating the staging directory, add:

```python
        total = sum(info.file_size for info in zf.infolist())
        if total > max_uncompressed_bytes:
            mb = max_uncompressed_bytes // (1024 * 1024)
            raise BundleUploadError(f"bundle would exceed max size of {mb} MB")
```

Place it immediately after `manifest = _read_manifest(zf, prefix)`.

- [ ] **Step 4: Run the failing tests again**

Run: `pytest tests/test_web_bundle_upload.py -v`
Expected: all pre-extraction rejection tests PASS. (`_extract_under_prefix` already raises `BundleUploadError("unsafe path: …")` for `..` entries, so the traversal test passes too.)

- [ ] **Step 5: Commit**

```bash
git add src/magsearch/web/bundle_upload.py tests/test_web_bundle_upload.py
git commit -m "Pre-extraction rejections: corrupt zip, missing/invalid manifest, size limit, path traversal."
```

---

## Task 6: `bundle_upload.py` — idempotent re-upload

If `bundles_dir/<id>/` already exists with the same `content_hash`, return that path without re-extracting.

**Files:**
- Modify: `src/magsearch/web/bundle_upload.py`
- Modify: `tests/test_web_bundle_upload.py`

- [ ] **Step 1: Add the test**

Append to `tests/test_web_bundle_upload.py`:

```python
def test_idempotent_reupload_returns_existing_path(tmp_path):
    src = make_bundle(tmp_path / "src")
    bundle_id = src.name
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    final_root = tmp_path / "final"
    final_root.mkdir()

    # First upload publishes the bundle.
    first = extract_and_stage(zip_path, final_root, max_uncompressed_bytes=_2_GB)
    assert first == final_root / bundle_id
    first_mtime = (first / "manifest.json").stat().st_mtime_ns

    # Second upload with identical content short-circuits.
    second = extract_and_stage(zip_path, final_root, max_uncompressed_bytes=_2_GB)
    assert second == first
    # The on-disk manifest mtime is unchanged — i.e. nothing got re-extracted.
    assert (second / "manifest.json").stat().st_mtime_ns == first_mtime
    # No leftover .upload-* staging directories.
    leftovers = [p.name for p in final_root.iterdir() if p.name.startswith(".upload-")]
    assert leftovers == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_web_bundle_upload.py::test_idempotent_reupload_returns_existing_path -v`
Expected: FAIL. The second `extract_and_stage` call will try `os.rename` into an existing directory and either raise or behave incorrectly.

- [ ] **Step 3: Add the collision pre-check**

Edit `src/magsearch/web/bundle_upload.py`. Inside `extract_and_stage`, after the size check and before creating the staging directory, add:

```python
        existing = bundles_dir / manifest.id
        if existing.exists():
            existing_manifest_path = existing / "manifest.json"
            if existing_manifest_path.exists():
                existing_manifest = Manifest.model_validate_json(
                    existing_manifest_path.read_text()
                )
                if existing_manifest.content_hash == manifest.content_hash:
                    return existing
            raise BundleUploadError(
                f"bundle id {manifest.id!r} already exists with different content"
            )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_web_bundle_upload.py::test_idempotent_reupload_returns_existing_path -v`
Expected: PASS.

- [ ] **Step 5: Run the full bundle_upload test file to confirm no regression**

Run: `pytest tests/test_web_bundle_upload.py -v`
Expected: all tests so far PASS.

- [ ] **Step 6: Commit**

```bash
git add src/magsearch/web/bundle_upload.py tests/test_web_bundle_upload.py
git commit -m "Idempotent re-upload: short-circuit when same content_hash already present."
```

---

## Task 7: `bundle_upload.py` — ID collision with different content

Cover the rejection path when an existing bundle has the same ID but a different `content_hash`. (Implementation is already present from Task 6's pre-check; this task adds the explicit test.)

**Files:**
- Modify: `tests/test_web_bundle_upload.py`

- [ ] **Step 1: Add the test**

Append to `tests/test_web_bundle_upload.py`:

```python
def test_id_collision_with_different_content_is_rejected(tmp_path):
    # Build two bundles that we'll force to share the same id.
    src_a = make_bundle(tmp_path / "src_a", num_pages=1)
    src_b = make_bundle(tmp_path / "src_b", num_pages=2)

    # Edit src_b's manifest to claim src_a's id (mimics a content-collision
    # situation). Note: this also changes the manifest's checksum file list,
    # but only the `id` field — bundle_upload's collision check only cares
    # about id and content_hash.
    manifest_a = json.loads((src_a / "manifest.json").read_text())
    manifest_b = json.loads((src_b / "manifest.json").read_text())
    manifest_b["id"] = manifest_a["id"]
    (src_b / "manifest.json").write_text(json.dumps(manifest_b))
    # Re-zip src_b with the doctored manifest. We also need src_b's bundle
    # directory to be renamed so zip_bundle's Shape A uses the new id.
    renamed = src_b.parent / manifest_a["id"]
    src_b.rename(renamed)
    zip_b = zip_bundle(renamed, tmp_path / "b.zip", shape="A")
    zip_a = zip_bundle(src_a, tmp_path / "a.zip", shape="A")

    final_root = tmp_path / "final"
    final_root.mkdir()

    extract_and_stage(zip_a, final_root, max_uncompressed_bytes=_2_GB)
    with pytest.raises(BundleUploadError, match="already exists with different content"):
        extract_and_stage(zip_b, final_root, max_uncompressed_bytes=_2_GB)

    # No residue.
    leftovers = [p.name for p in final_root.iterdir() if p.name.startswith(".upload-")]
    assert leftovers == []
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_web_bundle_upload.py::test_id_collision_with_different_content_is_rejected -v`
Expected: PASS. (Collision pre-check from Task 6 already handles this.)

If it fails, debug. The most likely reason is the doctored manifest no longer parses — adjust the test to also fix `content_hash` consistency if necessary, but Manifest schema does not cross-validate `content_hash` against listed checksums, so the test as written should work.

- [ ] **Step 3: Commit**

```bash
git add tests/test_web_bundle_upload.py
git commit -m "Verify ID collision with different content is rejected."
```

---

## Task 8: `bundle_upload.py` — post-extraction rejections

The checks that run after extracting into the staging directory: per-file checksum mismatch and "manifest references a file missing from the zip".

**Files:**
- Modify: `tests/test_web_bundle_upload.py`

- [ ] **Step 1: Add the tests**

Append to `tests/test_web_bundle_upload.py`:

```python
def test_checksum_mismatch_is_rejected(tmp_path):
    src = make_bundle(tmp_path / "src")
    # Corrupt one of the page files before zipping.
    corrupt = src / "pages" / "0001.webp"
    corrupt.write_bytes(b"corrupted bytes")
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    final_root = tmp_path / "final"
    final_root.mkdir()
    with pytest.raises(BundleUploadError, match="checksum mismatch"):
        extract_and_stage(zip_path, final_root, max_uncompressed_bytes=_2_GB)

    # No residue, no published bundle.
    assert list(final_root.iterdir()) == []


def test_missing_file_referenced_in_manifest_is_rejected(tmp_path):
    src = make_bundle(tmp_path / "src")
    # Delete a file the manifest still references.
    (src / "pages" / "0001.webp").unlink()
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    final_root = tmp_path / "final"
    final_root.mkdir()
    with pytest.raises(BundleUploadError, match="file listed in manifest is missing"):
        extract_and_stage(zip_path, final_root, max_uncompressed_bytes=_2_GB)

    assert list(final_root.iterdir()) == []
```

- [ ] **Step 2: Run the tests**

Run: `pytest tests/test_web_bundle_upload.py::test_checksum_mismatch_is_rejected tests/test_web_bundle_upload.py::test_missing_file_referenced_in_manifest_is_rejected -v`
Expected: PASS. (`_verify_checksums` already handles both.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_web_bundle_upload.py
git commit -m "Verify post-extraction checksum and missing-file rejections."
```

---

## Task 9: GET `/admin/issues/upload` — form template

The static form. POST handler comes next. No JS yet.

**Files:**
- Create: `src/magsearch/web/templates/admin/issue_upload.html`
- Modify: `src/magsearch/web/routes_admin.py`
- Modify: `tests/test_web_bundle_upload.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_bundle_upload.py`:

```python
# --- Route tests ---

def test_get_upload_form_requires_admin(app_client):
    client, _ = app_client
    resp = client.get("/admin/issues/upload", follow_redirects=False)
    # Anonymous → redirected to /login.
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_get_upload_form_renders_for_admin(admin_client):
    client, _ = admin_client
    resp = client.get("/admin/issues/upload")
    assert resp.status_code == 200
    assert 'name="bundle"' in resp.text
    assert 'type="file"' in resp.text
    assert 'name="csrf_token"' in resp.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_web_bundle_upload.py::test_get_upload_form_requires_admin tests/test_web_bundle_upload.py::test_get_upload_form_renders_for_admin -v`
Expected: FAIL — the route does not exist yet (404).

- [ ] **Step 3: Create the template**

Create `src/magsearch/web/templates/admin/issue_upload.html`:

```html
{% extends "admin/admin_base.html" %}

{% block admin_content %}
<div class="mb-8">
  <p class="caps caps-soft">Issues</p>
  <h2 class="display text-4xl">Upload bundle</h2>
  <p class="link-italic mt-2">
    Drop a <code>.zip</code> of a bundle directory built by
    <code>magsearch ingest</code> on the GPU box. The zip must contain a
    <code>manifest.json</code> at its root or inside a single top-level
    folder.
  </p>
</div>

{% if error %}
<div class="mb-6 border border-[var(--ink)] border-opacity-30 p-4">
  <p class="caps caps-soft">Upload rejected</p>
  <p>{{ error }}</p>
</div>
{% endif %}

<form method="post" action="/admin/issues/upload" enctype="multipart/form-data"
      class="flex flex-col gap-4 max-w-xl">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <input type="file" name="bundle" accept=".zip" required class="field-small">
  <div class="flex gap-3 items-baseline">
    <button class="mark-go" type="submit">upload ↵</button>
    <a href="/admin/issues" class="link-italic">cancel</a>
  </div>
</form>
{% endblock %}
```

- [ ] **Step 4: Add the GET route**

Edit `src/magsearch/web/routes_admin.py`. Add this route in the `# ------- Issues -------` section, after `issues_index`:

```python
@router.get("/issues/upload", response_class=HTMLResponse)
def issue_upload_form(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "admin/issue_upload.html", {"error": None}
    )
```

**Important:** place it *before* the `@router.get("/issues/{magazine_id}/edit", …)` route. FastAPI matches in registration order, and `upload` would otherwise be captured as a `magazine_id` value by the parameterized path.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `pytest tests/test_web_bundle_upload.py -v`
Expected: PASS for the two new route tests; all earlier tests still pass.

- [ ] **Step 6: Commit**

```bash
git add src/magsearch/web/templates/admin/issue_upload.html src/magsearch/web/routes_admin.py tests/test_web_bundle_upload.py
git commit -m "Add GET /admin/issues/upload form."
```

---

## Task 10: POST `/admin/issues/upload` — happy path

Wire up the handler: accept the multipart upload, stream it to a temp file, call `extract_and_stage`, call `import_bundle`, redirect.

**Files:**
- Modify: `src/magsearch/web/routes_admin.py`
- Modify: `tests/test_web_bundle_upload.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_bundle_upload.py`:

```python
from sqlalchemy import select

from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.models import Magazine
from magsearch.settings import get_settings


def _get_csrf(client) -> str:
    """Pull a CSRF token from the upload form."""
    resp = client.get("/admin/issues/upload")
    assert resp.status_code == 200
    import re
    m = re.search(r'name="csrf_token" value="([^"]+)"', resp.text)
    assert m, "csrf token not on upload form"
    return m.group(1)


def test_post_upload_imports_new_bundle(admin_client, tmp_path):
    client, bundles_dir = admin_client
    src = make_bundle(tmp_path / "src", title="ZX Spectrum", num_pages=1)
    bundle_id = src.name
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    token = _get_csrf(client)
    with zip_path.open("rb") as fp:
        resp = client.post(
            "/admin/issues/upload",
            data={"csrf_token": token},
            files={"bundle": ("upload.zip", fp, "application/zip")},
            follow_redirects=False,
        )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == f"/admin/issues/{bundle_id}/edit"

    # Bundle dir on disk.
    assert (bundles_dir / bundle_id / "manifest.json").exists()

    # DB row exists.
    settings = get_settings()
    factory = make_session_factory(make_engine(settings.database_url))
    with session_scope(factory) as s:
        mag = s.scalar(select(Magazine).where(Magazine.id == bundle_id))
        assert mag is not None
        assert mag.title == "ZX Spectrum"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_web_bundle_upload.py::test_post_upload_imports_new_bundle -v`
Expected: FAIL — POST handler doesn't exist yet (405 Method Not Allowed or 422).

- [ ] **Step 3: Add the POST route**

Edit `src/magsearch/web/routes_admin.py`. First add the necessary imports near the top:

```python
import shutil
import tempfile

from fastapi import UploadFile

from magsearch.importer import import_bundle, ImportError as MagImportError
from magsearch.web.bundle_upload import BundleUploadError, extract_and_stage
```

Then add the route immediately after `issue_upload_form`:

```python
@router.post("/issues/upload")
def issue_upload_submit(
    request: Request,
    bundle: UploadFile,
    _csrf: None = Depends(require_csrf),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    # Stream the upload to a real temp file so a 1 GB body doesn't OOM us.
    tmp_dir = Path(tempfile.mkdtemp(prefix="magsearch-upload-"))
    tmp_zip = tmp_dir / "upload.zip"
    try:
        with tmp_zip.open("wb") as out:
            shutil.copyfileobj(bundle.file, out)

        try:
            staged = extract_and_stage(
                tmp_zip,
                settings.bundles_dir,
                max_uncompressed_bytes=settings.max_upload_bytes,
            )
        except BundleUploadError as exc:
            return _TEMPLATES.TemplateResponse(
                request,
                "admin/issue_upload.html",
                {"error": str(exc)},
                status_code=400,
            )

        try:
            magazine_id = import_bundle(staged, db)
            db.commit()
        except MagImportError as exc:
            return _TEMPLATES.TemplateResponse(
                request,
                "admin/issue_upload.html",
                {"error": f"Import failed: {exc}"},
                status_code=400,
            )

        return RedirectResponse(
            url=f"/admin/issues/{magazine_id}/edit", status_code=303,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_web_bundle_upload.py::test_post_upload_imports_new_bundle -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/magsearch/web/routes_admin.py tests/test_web_bundle_upload.py
git commit -m "Add POST /admin/issues/upload happy path."
```

---

## Task 11: POST error rendering for `BundleUploadError`

Verify rejection cases surface in the form with status 400 and the error message in the page body.

**Files:**
- Modify: `tests/test_web_bundle_upload.py`

- [ ] **Step 1: Add the test**

Append to `tests/test_web_bundle_upload.py`:

```python
def test_post_upload_renders_error_on_bad_zip(admin_client, tmp_path):
    client, _ = admin_client
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"this is not a zip file")

    token = _get_csrf(client)
    with bad.open("rb") as fp:
        resp = client.post(
            "/admin/issues/upload",
            data={"csrf_token": token},
            files={"bundle": ("bad.zip", fp, "application/zip")},
            follow_redirects=False,
        )

    assert resp.status_code == 400
    assert "not a valid zip" in resp.text
    # The form is still on the page so the admin can retry.
    assert 'name="bundle"' in resp.text


def test_post_upload_renders_error_on_size_limit(admin_client, tmp_path, monkeypatch):
    # Cap the limit to 10 bytes for this test so any real bundle blows it.
    monkeypatch.setenv("MAGSEARCH_MAX_UPLOAD_BYTES", "10")
    # Reset cached settings/deps so the override takes effect.
    from magsearch.web import deps as _deps
    _deps._session_factory_for.cache_clear()

    client, _ = admin_client
    src = make_bundle(tmp_path / "src")
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    token = _get_csrf(client)
    with zip_path.open("rb") as fp:
        resp = client.post(
            "/admin/issues/upload",
            data={"csrf_token": token},
            files={"bundle": ("upload.zip", fp, "application/zip")},
            follow_redirects=False,
        )
    assert resp.status_code == 400
    assert "exceed max size" in resp.text
```

- [ ] **Step 2: Run the tests**

Run: `pytest tests/test_web_bundle_upload.py::test_post_upload_renders_error_on_bad_zip tests/test_web_bundle_upload.py::test_post_upload_renders_error_on_size_limit -v`
Expected: PASS for the first, the second test may fail if the size-limit override doesn't propagate. If it fails, see Step 3.

- [ ] **Step 3: If the size-limit test fails, investigate `get_settings` caching**

The default `get_settings` doesn't cache — it just constructs a new `Settings()` per call. But `get_db` uses an `lru_cache` keyed by `database_url`. If `monkeypatch.setenv` happens after the cached client is built, that's fine; only the new `Settings()` instance from `get_settings` matters here, and it reads the env var on construction.

If the test fails because the size limit override isn't observed, ensure `_csrf`/`get_db` aren't shadowing the settings — add a small debug print in `issue_upload_submit` for `settings.max_upload_bytes` to confirm.

- [ ] **Step 4: Commit**

```bash
git add tests/test_web_bundle_upload.py
git commit -m "Verify POST /admin/issues/upload surfaces validation errors in the form."
```

---

## Task 12: POST CSRF + auth checks

Verify the existing security middleware is wired up on the new POST.

**Files:**
- Modify: `tests/test_web_bundle_upload.py`

- [ ] **Step 1: Add the tests**

Append to `tests/test_web_bundle_upload.py`:

```python
def test_post_upload_requires_admin(user_client, tmp_path):
    """Signed-in non-admins are forbidden."""
    client, _ = user_client
    src = make_bundle(tmp_path / "src")
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    # We still need a CSRF token; user_client is signed in but not admin.
    # Grab one from /login (allow-listed, always renders a form).
    from tests.conftest import get_csrf
    token = get_csrf(client)

    with zip_path.open("rb") as fp:
        resp = client.post(
            "/admin/issues/upload",
            data={"csrf_token": token},
            files={"bundle": ("upload.zip", fp, "application/zip")},
            follow_redirects=False,
        )
    assert resp.status_code == 403


def test_post_upload_rejects_missing_csrf(admin_client, tmp_path):
    client, _ = admin_client
    src = make_bundle(tmp_path / "src")
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    with zip_path.open("rb") as fp:
        # No csrf_token in data.
        resp = client.post(
            "/admin/issues/upload",
            data={},
            files={"bundle": ("upload.zip", fp, "application/zip")},
            follow_redirects=False,
        )
    # FastAPI returns 422 if a required Form field is absent; the CSRF
    # dependency reads `csrf_token` via Form(...), so an absent token
    # triggers 422 (missing form field).
    assert resp.status_code in (403, 422)
```

- [ ] **Step 2: Run the tests**

Run: `pytest tests/test_web_bundle_upload.py::test_post_upload_requires_admin tests/test_web_bundle_upload.py::test_post_upload_rejects_missing_csrf -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_web_bundle_upload.py
git commit -m "Verify POST /admin/issues/upload enforces admin + CSRF."
```

---

## Task 13: POST idempotent re-upload redirects

Confirm that re-uploading the same zip produces a 303 to the existing issue and doesn't duplicate the DB row.

**Files:**
- Modify: `tests/test_web_bundle_upload.py`

- [ ] **Step 1: Add the test**

Append to `tests/test_web_bundle_upload.py`:

```python
def test_post_upload_idempotent_reupload(admin_client, tmp_path):
    client, bundles_dir = admin_client
    src = make_bundle(tmp_path / "src", title="Atari Age", num_pages=1)
    bundle_id = src.name
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    # First upload.
    token = _get_csrf(client)
    with zip_path.open("rb") as fp:
        first = client.post(
            "/admin/issues/upload",
            data={"csrf_token": token},
            files={"bundle": ("upload.zip", fp, "application/zip")},
            follow_redirects=False,
        )
    assert first.status_code == 303

    # Second upload of the identical zip → also redirects, no error.
    token = _get_csrf(client)
    with zip_path.open("rb") as fp:
        second = client.post(
            "/admin/issues/upload",
            data={"csrf_token": token},
            files={"bundle": ("upload.zip", fp, "application/zip")},
            follow_redirects=False,
        )
    assert second.status_code == 303
    assert second.headers["location"] == f"/admin/issues/{bundle_id}/edit"

    # Only one DB row.
    settings = get_settings()
    factory = make_session_factory(make_engine(settings.database_url))
    with session_scope(factory) as s:
        rows = s.scalars(select(Magazine).where(Magazine.id == bundle_id)).all()
        assert len(rows) == 1
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_web_bundle_upload.py::test_post_upload_idempotent_reupload -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_web_bundle_upload.py
git commit -m "Verify POST /admin/issues/upload is idempotent on re-upload."
```

---

## Task 14: Add upload progress JS to the template

Vanilla `XMLHttpRequest` with `upload.onprogress` writing into a `<progress>` element. No-JS fallback continues to work.

**Files:**
- Modify: `src/magsearch/web/templates/admin/issue_upload.html`

- [ ] **Step 1: Add progress UI + script**

Replace the form block in `src/magsearch/web/templates/admin/issue_upload.html` with:

```html
<form method="post" action="/admin/issues/upload" enctype="multipart/form-data"
      id="bundle-upload-form" class="flex flex-col gap-4 max-w-xl">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
  <input type="file" name="bundle" accept=".zip" required class="field-small">
  <progress id="bundle-upload-progress" value="0" max="100"
            class="hidden w-full"></progress>
  <p id="bundle-upload-status" class="caps caps-soft hidden"></p>
  <div class="flex gap-3 items-baseline">
    <button class="mark-go" type="submit" id="bundle-upload-submit">upload ↵</button>
    <a href="/admin/issues" class="link-italic">cancel</a>
  </div>
</form>

<script>
(function () {
  const form = document.getElementById("bundle-upload-form");
  const progress = document.getElementById("bundle-upload-progress");
  const status = document.getElementById("bundle-upload-status");
  const submit = document.getElementById("bundle-upload-submit");
  if (!form || !window.XMLHttpRequest || !window.FormData) return;

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    const fd = new FormData(form);
    const xhr = new XMLHttpRequest();
    xhr.open("POST", form.action);

    progress.classList.remove("hidden");
    status.classList.remove("hidden");
    submit.disabled = true;
    status.textContent = "uploading…";

    xhr.upload.addEventListener("progress", function (e) {
      if (!e.lengthComputable) return;
      const pct = Math.round((e.loaded / e.total) * 100);
      progress.value = pct;
      status.textContent = "uploading " + pct + "%";
    });
    xhr.addEventListener("load", function () {
      // On a 3xx the browser follows the Location header automatically when
      // the response body is the redirect. With XHR we have to do it manually.
      if (xhr.status >= 200 && xhr.status < 400) {
        status.textContent = "imported. redirecting…";
        // Follow the redirect target the server gave us; XHR exposes it via
        // responseURL when redirects are followed, or we fall back to issues.
        window.location = xhr.responseURL || "/admin/issues";
      } else {
        // Replace the page with the server-rendered error form.
        document.open();
        document.write(xhr.responseText);
        document.close();
      }
    });
    xhr.addEventListener("error", function () {
      status.textContent = "upload failed — network error";
      submit.disabled = false;
    });
    xhr.send(fd);
  });
})();
</script>
```

- [ ] **Step 2: Run the existing route tests to make sure they still pass**

Run: `pytest tests/test_web_bundle_upload.py -v`
Expected: all tests still PASS (the JS doesn't affect the no-JS test client behavior).

- [ ] **Step 3: Manual smoke check (optional but recommended)**

If a dev environment is available:
```bash
magsearch db upgrade
magsearch web --reload
# open http://127.0.0.1:8000/admin/issues/upload (sign in as admin first)
```
Drop a real bundle zip in the file picker and submit. Confirm:
- progress bar fills up
- redirect lands on /admin/issues/<id>/edit
- error case (corrupt zip) shows the error in the rendered form

If no dev environment is reachable, state this explicitly when reporting the task done.

- [ ] **Step 4: Commit**

```bash
git add src/magsearch/web/templates/admin/issue_upload.html
git commit -m "Add upload progress bar via XMLHttpRequest, with no-JS fallback."
```

---

## Task 15: Issues page — "Upload bundle" link

Add the entry point next to the search bar on `/admin/issues`.

**Files:**
- Modify: `src/magsearch/web/templates/admin/issues.html`
- Modify: `tests/test_web_bundle_upload.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_bundle_upload.py`:

```python
def test_issues_index_links_to_upload(admin_client):
    client, _ = admin_client
    resp = client.get("/admin/issues")
    assert resp.status_code == 200
    assert "/admin/issues/upload" in resp.text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_web_bundle_upload.py::test_issues_index_links_to_upload -v`
Expected: FAIL — issues.html doesn't yet link to /admin/issues/upload.

- [ ] **Step 3: Add the link to the template**

Edit `src/magsearch/web/templates/admin/issues.html`. Change the search form block at the top from:

```html
<form method="get" action="/admin/issues" class="mb-8 flex items-baseline gap-3">
  <input type="text" name="q" value="{{ q | default('', true) }}"
         placeholder="filter by title or issue…" class="field-small w-80">
  <button class="mark-go">go ↵</button>
</form>
```

…to:

```html
<div class="mb-8 flex items-baseline gap-6">
  <form method="get" action="/admin/issues" class="flex items-baseline gap-3">
    <input type="text" name="q" value="{{ q | default('', true) }}"
           placeholder="filter by title or issue…" class="field-small w-80">
    <button class="mark-go">go ↵</button>
  </form>
  <span class="flex-1"></span>
  <a href="/admin/issues/upload" class="mark-go">upload bundle ↑</a>
</div>
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_web_bundle_upload.py::test_issues_index_links_to_upload -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/magsearch/web/templates/admin/issues.html tests/test_web_bundle_upload.py
git commit -m "Link to bundle upload from the admin issues page."
```

---

## Task 16: Documentation

Three README additions: a new section, a config-table row, and an auth-section note.

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add the "Single-issue upload via the web UI" section**

Insert this section in `README.md` immediately *after* the "Ingesting magazines" section (before "Bulk ingestion"). The exact insertion point is right before the line `## Bulk ingestion`:

```markdown
## Single-issue upload via the web UI

For one-off issues, the admin web UI offers a faster path than `rsync` +
`magsearch import`. After producing a bundle on the GPU box:

```bash
cd ./bundles
zip -r byte-1985-12.zip byte-1985-12/
```

…sign in to the admin UI as an admin, go to **Issues → Upload bundle**, and
pick the zip. The server validates the manifest, verifies the per-file
checksums, stages the bundle into `bundles_dir/<id>/`, and runs the same
`import_bundle()` the CLI uses. Re-uploading the same zip is a no-op (you'll
land on the existing issue's edit page).

The bulk flow (`bulk-ingest` + `rsync` + `bulk-import`) is still the right
choice for ingesting many issues at once — the web upload is meant for a
single bundle at a time.
```

- [ ] **Step 2: Add the config-table row**

In the existing "Configuration" env-var table, add a new row after `MAGSEARCH_SESSION_SECRET`:

```markdown
| `MAGSEARCH_MAX_UPLOAD_BYTES` | `2147483648` | Cap on web bundle upload size, in bytes. Applies to both multipart body and uncompressed bundle contents. |
```

- [ ] **Step 3: Add the auth-section note**

Under "Authentication & admin", add a one-line note at the end of the introductory paragraph (the paragraph that ends with "…turn it on to gate the site behind a password."):

```markdown
The single-issue **Upload bundle** action under `/admin/issues` requires an
admin session regardless of the public/private toggle.
```

- [ ] **Step 4: Render-check the markdown locally**

Run: `grep -c "Upload bundle" README.md`
Expected: at least 2 occurrences.

Run: `grep "MAGSEARCH_MAX_UPLOAD_BYTES" README.md`
Expected: one line, showing the new row.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "Document single-issue bundle upload via the web UI."
```

---

## Task 17: Full test pass + final review

A final pass to make sure nothing regressed.

- [ ] **Step 1: Run the full test suite**

Run: `pytest -v`
Expected: all existing tests still pass; the new tests in `tests/test_web_bundle_upload.py` all pass.

If anything fails, fix the issue and re-commit before declaring done.

- [ ] **Step 2: Check no stray files**

Run: `git status`
Expected: working tree clean.

- [ ] **Step 3: Confirm the spec is fully covered**

Open `docs/superpowers/specs/2026-05-17-web-bundle-upload-design.md` and skim the section headings. Spot-check each requirement against the implementation:

- Architecture & user flow → Tasks 9, 10, 14
- Web routes & form → Tasks 9, 14
- Upload handler algorithm → Tasks 3–8
- Zip format & validation contract → Tasks 3–8
- Error handling → Tasks 11, 12
- Tests → present in `tests/test_web_bundle_upload.py`
- Documentation → Task 16

No new commits expected for this step; if a gap surfaces, file it as a follow-up issue rather than expanding the plan.
