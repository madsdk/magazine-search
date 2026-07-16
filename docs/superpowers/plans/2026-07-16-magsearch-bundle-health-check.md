# magsearch Bundle Health Check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `magsearch check` CLI command that audits bundles for missing files, corruption, empty/misaligned OCR, and database drift, cross-checking on-disk files, the manifest, and the database.

**Architecture:** A new pure module `src/magsearch/health.py` holds the check logic — `Finding`/`BundleReport` dataclasses and a `check_bundle()` that takes an already-parsed set of inputs and returns a report, plus two session-using helpers (`check_fts_integrity`, `iter_all_target_ids`). A thin `check` command in `cli.py` resolves targets (reusing `resolve_magazines`), loads each bundle's manifest + DB rows, calls `check_bundle`, prints a report, and computes the exit code. Findings are reported, never repaired.

**Tech Stack:** Python 3.12, Typer CLI, SQLAlchemy 2.x ORM, SQLite + FTS5, Pillow (image decode), Pydantic (manifest), pytest + Typer `CliRunner`.

## Global Constraints

- **Command shape mirrors `delete`:** `magsearch check [IDS...] [--title TEXT]`, IDs and `--title` combine as a union deduped by ID, `--title` matches the exact title case-insensitively. Reuse `magsearch.importer.resolve_magazines` — do not reimplement selection.
- **No selector = whole-corpus audit:** with no IDS and no `--title`, the target set is the union of all DB `Magazine` IDs and every subdirectory name under `settings.bundles_dir`.
- **Manifest is the on-disk source of truth.** The DB is cross-checked against it.
- **DB stores escaped page text.** `import_bundle` writes `Page.text = html.escape(manifest_entry.text)`. Any text comparison MUST compare against `html.escape(manifest_text)`, never the raw manifest text.
- **`pages_fts` is external-content FTS5** (`content='pages'`). Verify it with FTS5's `'integrity-check'` command run once per invocation — not per row.
- **Severity → exit code:** any ERROR (in any bundle, or the run-level FTS check) → exit 1. WARNINGs → exit 0 unless `--strict`, then exit 1. DB-open failure → exit 2. All healthy → exit 0.
- **`--checksums` is opt-in.** Presence, OCR-sanity, and image-decode run by default; full sha256 verification only with `--checksums`.
- **`health.py` must stay import-light** — it may import `PIL`, `magsearch.manifest`, `magsearch.models`, `magsearch.ingest.ids` (all confirmed free of `fitz`/`rarfile`), but MUST NOT import `magsearch.ingest.pipeline`/`bulk`/`ocr` or anything pulling in `fitz`/`rarfile`, so `cli.py` can import it at module top.
- **`check_bundle` is pure over its inputs** — it reads the bundle directory and receives DB rows as arguments; it opens no database session and performs no network I/O.
- **Read-only intent:** the command issues no data-mutating SQL. (The FTS `'integrity-check'` command inspects the index and does not modify data.)
- End git commit messages with the two trailer lines used elsewhere in this repo:
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01P1RCR5Hr6RtyFBh7CNSukK
  ```

---

## File Structure

- `src/magsearch/health.py` **(create)** — `Finding`, `BundleReport`, `check_bundle`, `check_fts_integrity`, `iter_all_target_ids`. One responsibility: computing bundle health. No CLI, no argument parsing.
- `src/magsearch/cli.py` **(modify)** — add the `check` command (orchestration + printing + exit code) and the needed imports. Reuse existing `_bundle_dir_size`/`_mb`? Not needed here.
- `tests/test_health.py` **(create)** — unit tests for `check_bundle` against staged fixture bundles + hand-built ORM rows. No live DB.
- `tests/test_cli_check.py` **(create)** — end-to-end `CliRunner` tests: exit codes, `--checksums`, `--strict`, whole-corpus orphans, unknown ID.
- `README.md` **(modify)** — document `check` next to `delete` / `ocr-rescale`.

---

## Task 1: `health.py` — data types and `check_bundle`

**Files:**
- Create: `src/magsearch/health.py`
- Test: `tests/test_health.py`

**Interfaces:**
- Consumes: `magsearch.manifest.Manifest`/`PageEntry`/`FileChecksum`; `magsearch.models.Magazine`/`Page`; `magsearch.ingest.ids.content_hash`.
- Produces (relied on by Task 2):
  - `Finding(level: str, message: str, page: int | None = None, path: str | None = None)` — `level` is `"error"` or `"warning"`.
  - `BundleReport(magazine_id: str, title: str | None, issue: str | None, page_count: int | None, findings: list[Finding])` with properties `errors -> list[Finding]`, `warnings -> list[Finding]`, `ok -> bool` (True iff no findings).
  - `check_bundle(bundle_dir: Path, magazine_id: str, mag_row: Magazine | None, page_rows: list[Page], *, verify_checksums: bool = False, decode_images: bool = True, bbox_tolerance: float = 0.02) -> BundleReport` — reads `bundle_dir`, returns a report. Loads `bundle_dir/manifest.json` itself; on missing/unparseable manifest, emits one ERROR and returns early (no per-file checks).

**Behavior details (implement exactly):**
- **Header fields** come from the manifest when it loaded, else from `mag_row`, else `title=None`/`issue=None`/`page_count=None`. `magazine_id` is always the passed value.
- **Manifest load:** if `bundle_dir/manifest.json` does not exist → `Finding("error", "manifest.json missing (bundle directory absent or incomplete)")`, return. If it exists but `Manifest.model_validate_json` raises → `Finding("error", f"manifest.json unparseable: {exc}")`, return.
- **File presence:** for each `PageEntry`, check `image_path`, `thumb_path`, `ocr_path` exist under `bundle_dir`; each missing → `Finding("error", "missing", page=entry.page_number, path=<rel>)`. Also check `original.{manifest.original_format}` exists (missing → ERROR, no page) and, when `manifest.cover_path` is non-empty, that it exists (missing → ERROR, no page).
- **Image decode** (only if `decode_images`): open each present page image, thumb, and the cover with `PIL.Image.open`; if it raises or `.size` has a zero dimension → `Finding("error", "unreadable image" / "image has zero dimension", page=<page or None>, path=<rel>)`. Skip files already reported missing.
- **Checksums** (only if `verify_checksums`): for each `manifest.checksums` entry, if the file exists and `content_hash(path) != entry.sha256` → `Finding("error", "checksum mismatch", path=entry.path)`. (Missing files are already covered by presence for page files and original/cover; a checksummed file that is missing and not otherwise covered → also ERROR "missing".)
- **OCR sanity:** for each `PageEntry` whose `ocr_path` exists, read + `json.loads`:
  - `JSONDecodeError` → `Finding("error", "unparseable OCR JSON", page, path)`.
  - value is a `list` → `Finding("warning", "legacy OCR format — run `magsearch ocr-rescale`", page, path)`; `regions = value`.
  - value is a `dict` with a `"regions"` key → `regions = value["regions"]`; if `"width"`/`"height"` present and numeric, flag out-of-bounds (below).
  - any other shape → `Finding("error", "unrecognized OCR JSON shape", page, path)`; skip region checks.
  - **empty:** if `len(regions) == 0` → `Finding("warning", "0 text regions (empty OCR)", page, path)`.
  - **out-of-bounds** (new format only, with numeric width `W`/height `H`): a region's `bbox=[x0,y0,x1,y1]` is out of bounds if `x0 < -W*tol` or `y0 < -H*tol` or `x1 > W*(1+tol)` or `y1 > H*(1+tol)`, where `tol = bbox_tolerance`. If any region is out of bounds → one `Finding("warning", f"{k} of {n} OCR regions have bboxes outside image bounds", page, path)`.
- **DB cross-check** (uses `mag_row`, `page_rows`, manifest):
  - `mag_row is None` → `Finding("error", "no database row for this magazine")`; skip remaining DB checks.
  - `mag_row.page_count != manifest.page_count` → `Finding("error", f"DB page_count {mag_row.page_count} != manifest {manifest.page_count}")`.
  - `db_nums = {p.page_number for p in page_rows}`; `man_nums = {e.page_number for e in manifest.pages}`; if `db_nums != man_nums` → `Finding("error", f"DB page numbers differ from manifest (missing from DB: {sorted(man_nums - db_nums)}, extra in DB: {sorted(db_nums - man_nums)})")`.
  - **text drift:** build `by_num = {p.page_number: p for p in page_rows}`; count pages where `by_num[e.page_number].text != html.escape(e.text)` for `e` in `manifest.pages` whose number is in `by_num`. If `k > 0` → one `Finding("warning", f"{k} page(s): DB text differs from manifest")`.
- **Finding order** within a report: file presence, image decode, checksums, OCR (in page order), then DB checks. (Order only affects display; tests assert membership, not order.)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_health.py`:

```python
import hashlib
import html
import json
from datetime import date
from pathlib import Path

from PIL import Image

from magsearch.health import check_bundle
from magsearch.manifest import FileChecksum, Manifest, PageEntry
from magsearch.models import Magazine, Page


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _webp(path: Path, size=(20, 26)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (255, 255, 255)).save(path, "WEBP")


def _write_ocr(path: Path, w=20, h=26, regions=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if regions is None:
        regions = [{"text": "hi", "bbox": [1, 1, 10, 10], "confidence": 0.9}]
    path.write_text(json.dumps({"width": w, "height": h, "regions": regions}))


def _stage(tmp_path: Path, mid="mag-1", npages=1, text="Page 1"):
    """Stage a clean bundle + matching manifest + DB rows. Return
    (bundle_dir, manifest, mag_row, page_rows)."""
    bundle = tmp_path / "bundles" / mid
    for sub in ("pages", "thumbs", "ocr"):
        (bundle / sub).mkdir(parents=True, exist_ok=True)
    entries = []
    for n in range(1, npages + 1):
        stem = f"{n:04d}"
        _webp(bundle / "pages" / f"{stem}.webp")
        _webp(bundle / "thumbs" / f"{stem}.webp", size=(10, 13))
        _write_ocr(bundle / "ocr" / f"{stem}.json")
        entries.append(PageEntry(
            page_number=n, image_path=f"pages/{stem}.webp",
            thumb_path=f"thumbs/{stem}.webp", ocr_path=f"ocr/{stem}.json", text=text,
        ))
    _webp(bundle / "cover.webp", size=(10, 13))
    (bundle / "original.pdf").write_bytes(b"%PDF-1.4 fake original bytes")
    checksums = [
        FileChecksum(path=str(p.relative_to(bundle)), sha256=_sha(p))
        for p in sorted(bundle.rglob("*"))
        if p.is_file() and p.name != "manifest.json"
    ]
    manifest = Manifest(
        schema_version=1, id=mid, title="Mag", issue="7",
        publication_date=date(1990, 1, 1), publisher=None,
        original_filename="original.pdf", original_format="pdf",
        page_count=npages, content_hash="deadbeef", ocr_engine="fake",
        ocr_engine_version="0", cover_path="cover.webp",
        pages=entries, checksums=checksums,
    )
    (bundle / "manifest.json").write_text(manifest.model_dump_json(indent=2))
    mag_row = Magazine(
        id=mid, title="Mag", issue="7", page_count=npages, content_hash="deadbeef",
        original_filename="original.pdf", original_format="pdf",
        cover_path=f"{mid}/cover.webp", ocr_engine="fake", ocr_engine_version="0",
    )
    page_rows = [
        Page(magazine_id=mid, page_number=e.page_number,
             image_path=f"{mid}/{e.image_path}", thumb_path=f"{mid}/{e.thumb_path}",
             text=html.escape(e.text))
        for e in entries
    ]
    return bundle, manifest, mag_row, page_rows


def _msgs(report):
    return [f.message for f in report.findings]


def test_clean_bundle_has_no_findings(tmp_path):
    bundle, _, mag, pages = _stage(tmp_path)
    report = check_bundle(bundle, "mag-1", mag, pages)
    assert report.ok, _msgs(report)
    assert report.errors == [] and report.warnings == []


def test_manifest_missing_is_single_error(tmp_path):
    bundle, _, mag, pages = _stage(tmp_path)
    (bundle / "manifest.json").unlink()
    report = check_bundle(bundle, "mag-1", mag, pages)
    assert len(report.errors) == 1
    assert "manifest.json missing" in report.errors[0].message


def test_missing_page_file_is_error(tmp_path):
    bundle, _, mag, pages = _stage(tmp_path)
    (bundle / "pages" / "0001.webp").unlink()
    report = check_bundle(bundle, "mag-1", mag, pages)
    assert any(f.level == "error" and f.path == "pages/0001.webp" for f in report.findings)


def test_undecodable_image_is_error(tmp_path):
    bundle, _, mag, pages = _stage(tmp_path)
    (bundle / "pages" / "0001.webp").write_bytes(b"not a webp")
    report = check_bundle(bundle, "mag-1", mag, pages)
    assert any(f.level == "error" and f.path == "pages/0001.webp" for f in report.findings)


def test_checksum_only_flagged_with_flag(tmp_path):
    bundle, _, mag, pages = _stage(tmp_path)
    # Mutate a non-image file so decode stays clean; only sha changes.
    (bundle / "original.pdf").write_bytes(b"%PDF-1.4 tampered")
    assert check_bundle(bundle, "mag-1", mag, pages).ok
    report = check_bundle(bundle, "mag-1", mag, pages, verify_checksums=True)
    assert any(f.level == "error" and "checksum" in f.message for f in report.findings)


def test_legacy_ocr_format_is_warning(tmp_path):
    bundle, _, mag, pages = _stage(tmp_path)
    (bundle / "ocr" / "0001.json").write_text(
        json.dumps([{"text": "x", "bbox": [1, 2, 3, 4], "confidence": 1.0}])
    )
    report = check_bundle(bundle, "mag-1", mag, pages)
    assert any(f.level == "warning" and "legacy" in f.message for f in report.findings)


def test_empty_ocr_is_warning(tmp_path):
    bundle, _, mag, pages = _stage(tmp_path)
    _write_ocr(bundle / "ocr" / "0001.json", regions=[])
    report = check_bundle(bundle, "mag-1", mag, pages)
    assert any(f.level == "warning" and "empty OCR" in f.message for f in report.findings)


def test_out_of_bounds_bbox_is_warning(tmp_path):
    bundle, _, mag, pages = _stage(tmp_path)
    _write_ocr(bundle / "ocr" / "0001.json",
               regions=[{"text": "x", "bbox": [1, 1, 999, 999], "confidence": 1.0}])
    report = check_bundle(bundle, "mag-1", mag, pages)
    assert any(f.level == "warning" and "outside image bounds" in f.message
               for f in report.findings)


def test_unparseable_ocr_is_error(tmp_path):
    bundle, _, mag, pages = _stage(tmp_path)
    (bundle / "ocr" / "0001.json").write_text("{not valid json")
    report = check_bundle(bundle, "mag-1", mag, pages)
    assert any(f.level == "error" and "OCR" in f.message for f in report.findings)


def test_db_row_missing_is_error(tmp_path):
    bundle, _, _, pages = _stage(tmp_path)
    report = check_bundle(bundle, "mag-1", None, [])
    assert any(f.level == "error" and "no database row" in f.message
               for f in report.findings)


def test_db_page_count_drift_is_error(tmp_path):
    bundle, _, mag, pages = _stage(tmp_path)
    mag.page_count = 5
    report = check_bundle(bundle, "mag-1", mag, pages)
    assert any(f.level == "error" and "page_count" in f.message for f in report.findings)


def test_db_page_numbers_drift_is_error(tmp_path):
    bundle, _, mag, pages = _stage(tmp_path)
    pages[0].page_number = 99
    report = check_bundle(bundle, "mag-1", mag, pages)
    assert any(f.level == "error" and "page numbers differ" in f.message
               for f in report.findings)


def test_db_text_drift_is_warning(tmp_path):
    bundle, _, mag, pages = _stage(tmp_path)
    pages[0].text = "completely different text"
    report = check_bundle(bundle, "mag-1", mag, pages)
    assert any(f.level == "warning" and "DB text differs" in f.message
               for f in report.findings)


def test_escaped_text_is_not_drift(tmp_path):
    # Manifest text with markup escapes to a different DB string; must NOT drift.
    bundle, _, mag, pages = _stage(tmp_path, text="A & B <tag>")
    report = check_bundle(bundle, "mag-1", mag, pages)
    assert not any("DB text differs" in f.message for f in report.findings), _msgs(report)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_health.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'magsearch.health'` (collection error).

- [ ] **Step 3: Implement `src/magsearch/health.py`**

```python
import html
import json
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from magsearch.ingest.ids import content_hash
from magsearch.manifest import Manifest
from magsearch.models import Magazine, Page


@dataclass
class Finding:
    level: str  # "error" | "warning"
    message: str
    page: int | None = None
    path: str | None = None


@dataclass
class BundleReport:
    magazine_id: str
    title: str | None
    issue: str | None
    page_count: int | None
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.findings


def check_bundle(
    bundle_dir: Path,
    magazine_id: str,
    mag_row: Magazine | None,
    page_rows: list[Page],
    *,
    verify_checksums: bool = False,
    decode_images: bool = True,
    bbox_tolerance: float = 0.02,
) -> BundleReport:
    findings: list[Finding] = []

    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.exists():
        title = mag_row.title if mag_row else None
        issue = mag_row.issue if mag_row else None
        page_count = mag_row.page_count if mag_row else None
        findings.append(Finding("error", "manifest.json missing (bundle directory absent or incomplete)"))
        return BundleReport(magazine_id, title, issue, page_count, findings)

    try:
        manifest = Manifest.model_validate_json(manifest_path.read_text())
    except Exception as exc:
        title = mag_row.title if mag_row else None
        issue = mag_row.issue if mag_row else None
        page_count = mag_row.page_count if mag_row else None
        findings.append(Finding("error", f"manifest.json unparseable: {exc}"))
        return BundleReport(magazine_id, title, issue, page_count, findings)

    # --- file presence ---
    image_files: list[tuple[int | None, str]] = []  # (page or None, rel path) to decode
    for entry in manifest.pages:
        for rel in (entry.image_path, entry.thumb_path, entry.ocr_path):
            if not (bundle_dir / rel).exists():
                findings.append(Finding("error", "missing", page=entry.page_number, path=rel))
        image_files.append((entry.page_number, entry.image_path))
        image_files.append((entry.page_number, entry.thumb_path))
    original_rel = f"original.{manifest.original_format}"
    if not (bundle_dir / original_rel).exists():
        findings.append(Finding("error", "missing", path=original_rel))
    if manifest.cover_path:
        if not (bundle_dir / manifest.cover_path).exists():
            findings.append(Finding("error", "missing", path=manifest.cover_path))
        else:
            image_files.append((None, manifest.cover_path))

    # --- image decode ---
    if decode_images:
        for page, rel in image_files:
            p = bundle_dir / rel
            if not p.exists():
                continue
            try:
                with Image.open(p) as im:
                    w, h = im.size
                    im.load()
            except Exception as exc:
                findings.append(Finding("error", f"unreadable image: {exc}", page=page, path=rel))
                continue
            if w == 0 or h == 0:
                findings.append(Finding("error", "image has zero dimension", page=page, path=rel))

    # --- checksums ---
    if verify_checksums:
        for c in manifest.checksums:
            p = bundle_dir / c.path
            if not p.exists():
                # Presence for page/original/cover files is covered above; guard others.
                findings.append(Finding("error", "missing", path=c.path))
                continue
            if content_hash(p) != c.sha256:
                findings.append(Finding("error", "checksum mismatch", path=c.path))

    # --- OCR sanity ---
    for entry in manifest.pages:
        p = bundle_dir / entry.ocr_path
        if not p.exists():
            continue  # already reported missing
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError as exc:
            findings.append(Finding("error", f"unparseable OCR JSON: {exc}",
                                    page=entry.page_number, path=entry.ocr_path))
            continue
        width = height = None
        if isinstance(data, list):
            findings.append(Finding("warning", "legacy OCR format — run `magsearch ocr-rescale`",
                                    page=entry.page_number, path=entry.ocr_path))
            regions = data
        elif isinstance(data, dict) and "regions" in data:
            regions = data["regions"]
            w, h = data.get("width"), data.get("height")
            if isinstance(w, (int, float)) and isinstance(h, (int, float)):
                width, height = float(w), float(h)
        else:
            findings.append(Finding("error", "unrecognized OCR JSON shape",
                                    page=entry.page_number, path=entry.ocr_path))
            continue
        if len(regions) == 0:
            findings.append(Finding("warning", "0 text regions (empty OCR)",
                                    page=entry.page_number, path=entry.ocr_path))
        if width is not None and height is not None:
            tol = bbox_tolerance
            oob = 0
            for r in regions:
                bbox = r.get("bbox") if isinstance(r, dict) else None
                if not (isinstance(bbox, list) and len(bbox) == 4):
                    continue
                x0, y0, x1, y1 = bbox
                if (x0 < -width * tol or y0 < -height * tol
                        or x1 > width * (1 + tol) or y1 > height * (1 + tol)):
                    oob += 1
            if oob:
                findings.append(Finding(
                    "warning",
                    f"{oob} of {len(regions)} OCR regions have bboxes outside image bounds",
                    page=entry.page_number, path=entry.ocr_path,
                ))

    # --- DB cross-check ---
    if mag_row is None:
        findings.append(Finding("error", "no database row for this magazine"))
    else:
        if mag_row.page_count != manifest.page_count:
            findings.append(Finding(
                "error", f"DB page_count {mag_row.page_count} != manifest {manifest.page_count}"))
        db_nums = {p.page_number for p in page_rows}
        man_nums = {e.page_number for e in manifest.pages}
        if db_nums != man_nums:
            findings.append(Finding(
                "error",
                f"DB page numbers differ from manifest "
                f"(missing from DB: {sorted(man_nums - db_nums)}, "
                f"extra in DB: {sorted(db_nums - man_nums)})"))
        by_num = {p.page_number: p for p in page_rows}
        drift = sum(
            1 for e in manifest.pages
            if e.page_number in by_num and by_num[e.page_number].text != html.escape(e.text)
        )
        if drift:
            findings.append(Finding("warning", f"{drift} page(s): DB text differs from manifest"))

    return BundleReport(
        magazine_id,
        manifest.title,
        manifest.issue,
        manifest.page_count,
        findings,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_health.py -q`
Expected: PASS (all tests).

- [ ] **Step 5: Lint**

Run: `.venv/bin/ruff check src/magsearch/health.py tests/test_health.py`
Expected: no new errors (pre-existing repo style aside).

- [ ] **Step 6: Commit**

```bash
git add src/magsearch/health.py tests/test_health.py
git commit -m "feat: bundle health check core (check_bundle + reports)"
```

---

## Task 2: `magsearch check` CLI command + session helpers

**Files:**
- Modify: `src/magsearch/health.py` (add `check_fts_integrity`, `iter_all_target_ids`)
- Modify: `src/magsearch/cli.py` (add `check` command + imports)
- Test: `tests/test_cli_check.py`

**Interfaces:**
- Consumes: `check_bundle`, `Finding`, `BundleReport` from Task 1; `resolve_magazines` from `magsearch.importer`; `Magazine` from `magsearch.models`; `get_settings`, `make_engine`, `make_session_factory`, `session_scope`.
- Produces:
  - `check_fts_integrity(session) -> Finding | None` — runs FTS5 `'integrity-check'`; returns a run-level ERROR `Finding` on failure, else `None`.
  - `iter_all_target_ids(session, bundles_dir: Path) -> list[str]` — sorted union of all DB `Magazine.id` values and every subdirectory name under `bundles_dir`.
  - CLI command `check` registered on `app`.

**CLI behavior (implement exactly):**
- Signature:
  ```python
  @app.command("check")
  def check_cmd(
      ids: Annotated[list[str] | None, typer.Argument(help="Magazine IDs to check. With no IDs and no --title, checks every bundle.")] = None,
      title: Annotated[str | None, typer.Option("--title", help="Also check every issue with this exact title (case-insensitive).")] = None,
      checksums: Annotated[bool, typer.Option("--checksums", help="Also verify every file's sha256 against the manifest (slow).")] = False,
      strict: Annotated[bool, typer.Option("--strict", help="Treat warnings as failures for the exit code.")] = False,
  ) -> None:
  ```
- Build engine/session as `delete` does. Wrap the DB work in `try/except OperationalError` → print to stderr, exit 2.
- Run `check_fts_integrity(s)` first; if it returns a finding, print `f"ERROR  {finding.message}"` and remember `any_error = True`.
- Resolve targets:
  - If `ids or title`: `targets = resolve_magazines(s, ids, title)`; for each `targets.not_found` print `f"ERROR  {missing} — no such magazine"` and set `any_error = True`; `target_ids = [m.id for m in targets.found]`; if `not target_ids and not targets.not_found`: print `"no matching magazines"` to stderr and `raise typer.Exit(code=1)`.
  - Else: `target_ids = iter_all_target_ids(s, settings.bundles_dir)`; if empty, print `"no bundles found"` to stderr and `raise typer.Exit(code=1)`.
- For each `mid` in `target_ids`: `mag_row = s.get(Magazine, mid)`, `page_rows = list(mag_row.pages) if mag_row else []`, `report = check_bundle(settings.bundles_dir / mid, mid, mag_row, page_rows, verify_checksums=checksums)`; print it (helper below); tally `clean` / `warn-only` / `error` buckets and total error/warning counts; set `any_error`/`any_warning`.
- Print summary: `f"Summary: {n} bundle(s) — {clean} OK, {warn} with warnings, {err} with errors ({total_errors} error(s), {total_warnings} warning(s))"`.
- Exit code: `1` if `any_error`; elif `strict and any_warning` → `1`; else `0`. Only `raise typer.Exit(code=1)` when non-zero (returning normally = exit 0).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_check.py`:

```python
import json
import shutil
from pathlib import Path

from typer.testing import CliRunner

from magsearch.cli import app

runner = CliRunner()


def _env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    bundles = tmp_path / "bundles"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(bundles))
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    return bundles


def _ingest_import(tmp_path, bundles, title, date_str):
    from tests.fixtures.pdfs import make_pdf
    src = make_pdf(tmp_path / f"{title}-{date_str}.pdf", num_pages=2)
    r = runner.invoke(app, [
        "ingest", str(src), "--title", title, "--date", date_str,
        "--bundles-dir", str(bundles), "--fake-ocr",
    ])
    assert r.exit_code == 0, r.stdout
    slug = f"{title.lower()}-{date_str[:7]}"
    r = runner.invoke(app, ["import", str(bundles / slug)])
    assert r.exit_code == 0, r.stdout
    return slug


def test_clean_bundle_exits_zero(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    r = runner.invoke(app, ["check", slug])
    assert r.exit_code == 0, r.stdout
    assert "OK" in r.stdout


def test_missing_file_exits_one(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    (bundles / slug / "pages" / "0001.webp").unlink()
    r = runner.invoke(app, ["check", slug])
    assert r.exit_code == 1, r.stdout
    assert "0001.webp" in r.stdout


def test_checksums_flag_catches_corruption(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    # Corrupt a non-image file so only the checksum check fires.
    (bundles / slug / "original.pdf").write_bytes(b"tampered")
    assert runner.invoke(app, ["check", slug]).exit_code == 0
    r = runner.invoke(app, ["check", slug, "--checksums"])
    assert r.exit_code == 1, r.stdout
    assert "checksum" in r.stdout


def test_strict_promotes_warning_to_failure(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    # Rewrite one OCR file to legacy (array) format => warning, not error.
    (bundles / slug / "ocr" / "0001.json").write_text(
        json.dumps([{"text": "x", "bbox": [1, 2, 3, 4], "confidence": 1.0}])
    )
    assert runner.invoke(app, ["check", slug]).exit_code == 0
    r = runner.invoke(app, ["check", slug, "--strict"])
    assert r.exit_code == 1, r.stdout
    assert "legacy" in r.stdout


def test_whole_corpus_flags_orphan_dir(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    (bundles / "orphan-2000-01").mkdir()  # dir on disk, no DB row, no manifest
    r = runner.invoke(app, ["check"])  # no selector => audit everything
    assert r.exit_code == 1, r.stdout
    assert "orphan-2000-01" in r.stdout


def test_whole_corpus_flags_orphan_db_row(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    shutil.rmtree(bundles / slug)  # DB row remains, directory gone
    r = runner.invoke(app, ["check"])
    assert r.exit_code == 1, r.stdout
    assert slug in r.stdout


def test_unknown_id_exits_one(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    r = runner.invoke(app, ["check", "does-not-exist"])
    assert r.exit_code == 1, r.stdout
    assert "does-not-exist" in r.stdout
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli_check.py -q`
Expected: FAIL — `check` command not registered (Typer usage error / non-matching exit codes).

- [ ] **Step 3: Add the session helpers to `src/magsearch/health.py`**

Add these imports at the top of `health.py` (alongside the existing ones):

```python
from sqlalchemy import select, text
from sqlalchemy.orm import Session
```

Append to `health.py`:

```python
def check_fts_integrity(session: "Session") -> Finding | None:
    """Verify the pages_fts index is consistent with the pages content table.

    pages_fts is an external-content FTS5 table; its built-in 'integrity-check'
    command validates the index against pages. Returns a run-level error
    Finding on failure, else None. Reads only — does not modify data.
    """
    try:
        session.execute(text("INSERT INTO pages_fts(pages_fts) VALUES ('integrity-check')"))
        return None
    except Exception as exc:
        return Finding("error", f"pages_fts integrity check failed: {exc}")


def iter_all_target_ids(session: "Session", bundles_dir: Path) -> list[str]:
    """Union of all DB magazine IDs and every subdirectory under bundles_dir."""
    ids: set[str] = set(session.scalars(select(Magazine.id)))
    if bundles_dir.exists():
        ids |= {p.name for p in bundles_dir.iterdir() if p.is_dir()}
    return sorted(ids)
```

- [ ] **Step 4: Add the `check` command to `src/magsearch/cli.py`**

Update the `magsearch.models` import (currently only `User`):

```python
from magsearch.models import Magazine, User
```

Add the health import near the other `magsearch` imports:

```python
from magsearch.health import check_bundle, check_fts_integrity, iter_all_target_ids
```

Add the `OperationalError` import:

```python
from sqlalchemy.exc import OperationalError
```

Add this command (place it after `delete_cmd`):

```python
def _print_report(report) -> None:
    header = report.magazine_id
    if report.title:
        header += f" — {report.title}"
    if report.issue:
        header += f" №{report.issue}"
    if report.page_count is not None:
        header += f" ({report.page_count} pages)"
    if report.ok:
        typer.echo(f"{header} : OK")
        return
    typer.echo(header)
    for f in report.findings:
        loc = f"page {f.page:04d} " if f.page is not None else ""
        path = f"{f.path} — " if f.path else ""
        typer.echo(f"  {f.level.upper():5} {loc}{path}{f.message}")
    typer.echo(f"  {len(report.errors)} error(s), {len(report.warnings)} warning(s)")


@app.command("check")
def check_cmd(
    ids: Annotated[list[str] | None, typer.Argument(help="Magazine IDs to check. With no IDs and no --title, checks every bundle.")] = None,
    title: Annotated[str | None, typer.Option("--title", help="Also check every issue with this exact title (case-insensitive).")] = None,
    checksums: Annotated[bool, typer.Option("--checksums", help="Also verify every file's sha256 against the manifest (slow).")] = False,
    strict: Annotated[bool, typer.Option("--strict", help="Treat warnings as failures for the exit code.")] = False,
) -> None:
    """Audit bundles for missing files, corruption, and OCR problems."""
    ids = ids or []
    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)

    any_error = False
    any_warning = False
    clean = warn_only = with_errors = 0
    total_errors = total_warnings = 0

    try:
        with session_scope(factory) as s:
            fts = check_fts_integrity(s)
            if fts is not None:
                typer.echo(f"ERROR  {fts.message}")
                any_error = True
                total_errors += 1

            if ids or title:
                targets = resolve_magazines(s, ids, title)
                for missing in targets.not_found:
                    typer.echo(f"ERROR  {missing} — no such magazine")
                    any_error = True
                    total_errors += 1
                target_ids = [m.id for m in targets.found]
                if not target_ids and not targets.not_found:
                    typer.echo("no matching magazines", err=True)
                    raise typer.Exit(code=1)
            else:
                target_ids = iter_all_target_ids(s, settings.bundles_dir)
                if not target_ids:
                    typer.echo("no bundles found", err=True)
                    raise typer.Exit(code=1)

            for mid in target_ids:
                mag_row = s.get(Magazine, mid)
                page_rows = list(mag_row.pages) if mag_row is not None else []
                report = check_bundle(
                    settings.bundles_dir / mid, mid, mag_row, page_rows,
                    verify_checksums=checksums,
                )
                _print_report(report)
                total_errors += len(report.errors)
                total_warnings += len(report.warnings)
                if report.errors:
                    with_errors += 1
                    any_error = True
                elif report.warnings:
                    warn_only += 1
                    any_warning = True
                else:
                    clean += 1
    except OperationalError as exc:
        typer.echo(f"database error: {exc}", err=True)
        raise typer.Exit(code=2)

    n = clean + warn_only + with_errors
    typer.echo(
        f"Summary: {n} bundle(s) — {clean} OK, {warn_only} with warnings, "
        f"{with_errors} with errors ({total_errors} error(s), {total_warnings} warning(s))"
    )

    if any_error:
        raise typer.Exit(code=1)
    if strict and any_warning:
        raise typer.Exit(code=1)
```

Note: `typer.Exit` raised inside the `with` block for the empty-target cases will propagate through the `except OperationalError` (it is not an `OperationalError`), giving exit 1 as intended.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cli_check.py -q`
Expected: PASS (all tests).

- [ ] **Step 6: Run the full suite + lint**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check src/magsearch/health.py src/magsearch/cli.py tests/test_cli_check.py`
Expected: full suite passes (previous 291 passed + new tests); no new ruff errors.

- [ ] **Step 7: Commit**

```bash
git add src/magsearch/health.py src/magsearch/cli.py tests/test_cli_check.py
git commit -m "feat: magsearch check CLI command for bundle health"
```

---

## Task 3: Document `check` in the README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add a "Checking bundle health" section**

Locate the CLI reference area near the existing "Deleting magazines" and `ocr-rescale` documentation. Add a section documenting `magsearch check`, matching the surrounding style:

```markdown
### Checking bundle health

Audit bundles for the damage a bad ingestion can leave behind — missing page
images/thumbnails/OCR, corrupt files, empty or misaligned OCR, and drift
between the database and the on-disk bundle:

```
magsearch check <magazine_id>            # one bundle
magsearch check --title "Computer Gaming World"
magsearch check                          # audit every bundle
```

For each bundle it prints `OK` or the specific problems found, then a summary,
and exits non-zero if any bundle has an error.

Options:

- `--checksums` — also recompute every file's SHA-256 and compare it to the
  manifest. The strongest corruption check, but it reads every byte, so it is
  off by default.
- `--strict` — treat warnings (e.g. legacy-format OCR that `magsearch
  ocr-rescale` would fix, empty-text pages) as failures for the exit code.

`check` only reports; it never modifies bundles. Fix misaligned highlights with
`magsearch ocr-rescale`, and re-ingest to recover missing pages or empty OCR.
```

(Adjust heading level and fenced-block nesting to match the README's existing conventions.)

- [ ] **Step 2: Verify the docs render / read correctly**

Run: `git diff README.md`
Expected: a single new section, correct Markdown, consistent with neighbors.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document magsearch check command"
```

---

## Self-Review

- **Spec coverage:** File presence ✅ (Task 1 presence checks). OCR sanity — legacy/empty/out-of-bounds/unparseable ✅ (Task 1). Image decode ✅ (Task 1). Checksums opt-in ✅ (Task 1 `verify_checksums`; Task 2 `--checksums`). DB cross-check (row/count/page-numbers/text drift) ✅ (Task 1). FTS integrity ✅ (Task 2 `check_fts_integrity`). Whole-corpus union + orphans ✅ (Task 2 `iter_all_target_ids`, exercised by orphan tests). Selection mirrors delete ✅ (reuses `resolve_magazines`). Report + exit codes + `--strict` ✅ (Task 2). README ✅ (Task 3).
- **Type consistency:** `Finding`/`BundleReport` fields and `check_bundle` signature are defined once in Task 1 and consumed unchanged in Task 2. `check_fts_integrity`/`iter_all_target_ids` signatures match their Task 2 call sites. `html.escape` comparison matches `import_bundle`. `resolve_magazines` used with its real `(session, ids, title)` signature.
- **Placeholder scan:** none — every code and test step is complete.
- **Import-weight constraint:** `health.py` imports only `PIL`, `magsearch.manifest`, `magsearch.models`, `magsearch.ingest.ids`, and SQLAlchemy — all confirmed free of `fitz`/`rarfile`, so `cli.py`'s top-level import of it is safe.
