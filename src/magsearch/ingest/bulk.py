"""Bulk ingestion: iterate over a directory of magazine files, run the per-file
IngestPipeline on each, and persist progress so crashes don't restart the batch
from zero.

The orchestrator is a thin wrapper around `IngestPipeline.run()` — no new
ingestion logic. The pieces here are:

  * `SidecarRow` / `load_sidecar`  — per-file metadata source (CSV or JSON).
  * `StateLog`                     — JSONL log of per-file status across runs.
  * `bulk_ingest`                  — the loop that wires it all together.
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from magsearch.ingest.ocr import FatalOCRError, OCREngine
from magsearch.ingest.pipeline import IngestOptions, IngestPipeline, IngestResult

_INGESTIBLE_SUFFIXES = {".pdf", ".cbz", ".cbr"}
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SidecarRow:
    """Per-file metadata as supplied by the user's sidecar CSV/JSON.

    All fields except `filename` are optional. Mirrors the flags accepted by
    `magsearch ingest`.
    """
    filename: str
    title: str = ""
    issue: str | None = None
    date: date | None = None
    publisher: str | None = None
    id: str | None = None

    def to_options(self) -> IngestOptions:
        return IngestOptions(
            title=self.title,
            issue=self.issue or None,
            publication_date=self.date,
            publisher=self.publisher or None,
            id_override=self.id or None,
        )


class SidecarError(ValueError):
    """Raised when the sidecar is missing required structure or references
    files that don't exist in the input directory."""


def _coerce_row(raw: dict, lineno: int | None = None) -> SidecarRow:
    """Validate and normalize one sidecar row dict into a SidecarRow."""
    where = f"row {lineno}" if lineno is not None else "row"
    filename = (raw.get("filename") or "").strip()
    if not filename:
        raise SidecarError(f"{where}: 'filename' is required")
    date_str = (raw.get("date") or "").strip() or None
    parsed_date: date | None = None
    if date_str:
        try:
            parsed_date = date.fromisoformat(date_str)
        except ValueError as exc:
            raise SidecarError(f"{where}: bad date {date_str!r}: {exc}") from None
    return SidecarRow(
        filename=filename,
        title=(raw.get("title") or "").strip(),
        issue=(raw.get("issue") or "").strip() or None,
        date=parsed_date,
        publisher=(raw.get("publisher") or "").strip() or None,
        id=(raw.get("id") or "").strip() or None,
    )


def load_sidecar(path: Path) -> dict[str, SidecarRow]:
    """Read a CSV (default) or JSON sidecar file into {filename: SidecarRow}.

    Format is selected by file extension. Duplicate filenames raise
    SidecarError; blank lines and lines starting with '#' are tolerated in CSV.
    """
    suffix = path.suffix.lower()
    rows: list[SidecarRow]
    if suffix == ".json":
        data = json.loads(path.read_text())
        if not isinstance(data, list):
            raise SidecarError(f"{path}: top-level JSON must be a list of objects")
        rows = [_coerce_row(r, i + 1) for i, r in enumerate(data)]
    else:
        rows = []
        with path.open(newline="") as f:
            # csv.DictReader handles the header line; skip lines that start with '#'
            stripped = (ln for ln in f if not ln.lstrip().startswith("#"))
            reader = csv.DictReader(stripped)
            if reader.fieldnames is None or "filename" not in reader.fieldnames:
                raise SidecarError(f"{path}: CSV must have a 'filename' column")
            for i, raw in enumerate(reader, start=2):  # row 1 was the header
                if not any((v or "").strip() for v in raw.values()):
                    continue
                rows.append(_coerce_row(raw, i))
    by_name: dict[str, SidecarRow] = {}
    for row in rows:
        if row.filename in by_name:
            raise SidecarError(f"duplicate filename in sidecar: {row.filename!r}")
        by_name[row.filename] = row
    return by_name


def scan_inputs(input_dir: Path, *, recursive: bool) -> list[Path]:
    """List ingestible files (PDF/CBR/CBZ) in `input_dir`, sorted by name."""
    if not input_dir.is_dir():
        raise FileNotFoundError(f"input dir not found: {input_dir}")
    pattern = "**/*" if recursive else "*"
    files = [
        p for p in input_dir.glob(pattern)
        if p.is_file() and p.suffix.lower() in _INGESTIBLE_SUFFIXES
    ]
    return sorted(files)


# --- State log -------------------------------------------------------------

PENDING = "pending"
IN_PROGRESS = "in_progress"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass
class StateEntry:
    filename: str
    status: str = PENDING
    attempts: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    content_hash: str | None = None
    bundle_id: str | None = None
    error: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class StateLog:
    """JSONL state file recording per-file status across bulk-ingest runs.

    The whole file is rewritten on every mutation. Files-per-collection is in
    the thousands at most — a full rewrite of a few hundred KB on each update
    is cheap and keeps the file consistent without locking.
    """

    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, StateEntry] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                self.entries[raw["filename"]] = StateEntry(**raw)

    def get(self, filename: str) -> StateEntry:
        return self.entries.setdefault(filename, StateEntry(filename=filename))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        lines = [json.dumps(asdict(e)) for e in self.entries.values()]
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""))
        tmp.replace(self.path)

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries.values():
            out[e.status] = out.get(e.status, 0) + 1
        return out


# --- Orchestrator ----------------------------------------------------------

@dataclass
class BulkIngestResult:
    processed: int
    succeeded: int
    failed: int
    skipped: int
    state_file: Path
    bundles: list[Path] = field(default_factory=list)
    # True when the run stopped early on an unrecoverable OCR error (e.g. a
    # fatal GPU fault) rather than running to completion. Remaining files are
    # left untouched — restart the process and re-run with retry_failed.
    aborted: bool = False


@dataclass
class _Plan:
    """One file's plan resolved against the sidecar + prior state."""
    path: Path
    options: IngestOptions
    prior_status: str | None  # None when this file has no state entry yet


def _resolve_plan(
    files: list[Path],
    input_dir: Path,
    sidecar: dict[str, SidecarRow],
    state: StateLog,
    *,
    strict_sidecar: bool,
    retry_failed: bool,
    on_warning: Callable[[str], None],
) -> tuple[list[_Plan], list[_Plan]]:
    """Split files into (to_process, to_skip), validating sidecar coverage."""
    # Sidecar entries must reference files that actually exist.
    file_keys = {str(p.relative_to(input_dir)) for p in files}
    for name in sidecar:
        if name not in file_keys:
            raise SidecarError(
                f"sidecar references {name!r} but no such file in {input_dir}"
            )

    process: list[_Plan] = []
    skip: list[_Plan] = []
    for path in files:
        key = str(path.relative_to(input_dir))
        row = sidecar.get(key)
        if row is None:
            msg = f"no sidecar row for {key} — using defaults (title={path.stem})"
            if strict_sidecar:
                raise SidecarError(msg)
            on_warning(msg)
            row = SidecarRow(filename=key, title=path.stem)
        entry = state.get(key)
        plan = _Plan(path=path, options=row.to_options(), prior_status=entry.status)
        # Decide based on the entry's recorded status.
        if entry.status == DONE:
            skip.append(plan)
        elif entry.status == FAILED and not retry_failed:
            skip.append(plan)
        else:
            process.append(plan)
    return process, skip


def bulk_ingest(
    *,
    input_dir: Path,
    sidecar_path: Path | None,
    state_path: Path | None,
    bundles_dir: Path,
    ocr_engine: OCREngine,
    recursive: bool = False,
    strict_sidecar: bool = False,
    retry_failed: bool = False,
    force_all: bool = False,
    halt_on_error: bool = False,
    dry_run: bool = False,
    on_warning: Callable[[str], None] = lambda msg: None,
    on_file_start: Callable[[int, int, Path], None] = lambda i, n, p: None,
    on_file_end: Callable[[int, int, Path, StateEntry], None] = lambda i, n, p, e: None,
    on_page: Callable[[int, int], None] | None = None,
) -> BulkIngestResult:
    """Run IngestPipeline across every ingestible file in `input_dir`."""
    files = scan_inputs(input_dir, recursive=recursive)

    state_path = state_path or (input_dir / ".bulk-state.jsonl")
    state = StateLog(state_path)

    if sidecar_path is not None:
        sidecar = load_sidecar(sidecar_path)
    else:
        default_csv = input_dir / "metadata.csv"
        default_json = input_dir / "metadata.json"
        if default_csv.exists():
            sidecar = load_sidecar(default_csv)
        elif default_json.exists():
            sidecar = load_sidecar(default_json)
        else:
            sidecar = {}
            on_warning(
                "no sidecar found — every file will use filename as title and "
                "auto-generated (hash-based) IDs"
            )

    to_process, to_skip = _resolve_plan(
        files, input_dir, sidecar, state,
        strict_sidecar=strict_sidecar, retry_failed=retry_failed,
        on_warning=on_warning,
    )

    if dry_run:
        for plan in to_process:
            on_warning(f"[dry-run] would ingest: {plan.path.relative_to(input_dir)}")
        for plan in to_skip:
            on_warning(f"[dry-run] would skip: {plan.path.relative_to(input_dir)}")
        return BulkIngestResult(
            processed=0,
            succeeded=0,
            failed=0,
            skipped=len(to_skip),
            state_file=state_path,
        )

    bundles: list[Path] = []
    succeeded = failed = 0
    aborted = False
    total = len(to_process) + len(to_skip)

    # Replay the skipped files so callers can render them in order.
    for idx, plan in enumerate(to_skip, start=1):
        entry = state.get(str(plan.path.relative_to(input_dir)))
        on_file_start(idx, total, plan.path)
        on_file_end(idx, total, plan.path, entry)

    for offset, plan in enumerate(to_process, start=1):
        idx = len(to_skip) + offset
        key = str(plan.path.relative_to(input_dir))
        entry = state.get(key)
        entry.attempts += 1
        entry.status = IN_PROGRESS
        entry.started_at = _now_iso()
        entry.error = None
        state.save()
        on_file_start(idx, total, plan.path)
        try:
            pipeline = IngestPipeline(
                bundles_root=bundles_dir,
                ocr_engine=ocr_engine,
                options=plan.options,
            )
            result: IngestResult = pipeline.run(
                plan.path, force=force_all, on_page=on_page,
            )
            entry.status = DONE
            entry.bundle_id = result.id
            entry.content_hash = result.manifest.content_hash
            entry.error = None
            succeeded += 1
            bundles.append(result.bundle_dir)
        except FatalOCRError as exc:
            # The OCR engine's state is corrupt for the rest of this process, so
            # every remaining file would fail the same way (or worse, silently
            # produce empty-text bundles). Record this file as failed and stop
            # the whole run regardless of halt_on_error — the operator must
            # restart the process and re-run with retry_failed to resume.
            entry.status = FAILED
            entry.error = f"{type(exc).__name__}: {exc}"
            failed += 1
            aborted = True
            _LOG.error(
                "fatal OCR error on %s — aborting bulk run; the OCR engine "
                "cannot recover in-process. Restart and re-run with "
                "retry_failed to resume. Cause: %s",
                plan.path, exc,
            )
            msg = (
                f"fatal OCR error on {key} — aborting run. The OCR engine "
                "cannot recover in-process (a GPU fault leaves the process in "
                "an inconsistent state). Restart and re-run with --retry-failed "
                "to resume from here; no further files were processed."
            )
            on_warning(msg)
            entry.finished_at = _now_iso()
            state.save()
            on_file_end(idx, total, plan.path, entry)
            break
        except Exception as exc:  # noqa: BLE001 — bulk runs need to record any failure
            entry.status = FAILED
            entry.error = f"{type(exc).__name__}: {exc}"
            failed += 1
            _LOG.exception("ingest failed for %s", plan.path)
            entry.finished_at = _now_iso()
            state.save()
            on_file_end(idx, total, plan.path, entry)
            if halt_on_error:
                break
            continue
        entry.finished_at = _now_iso()
        state.save()
        on_file_end(idx, total, plan.path, entry)

    return BulkIngestResult(
        processed=succeeded + failed,
        succeeded=succeeded,
        failed=failed,
        skipped=len(to_skip),
        state_file=state_path,
        bundles=bundles,
        aborted=aborted,
    )


# Re-export so callers don't need a separate import path.
__all__ = [
    "BulkIngestResult", "SidecarRow", "SidecarError",
    "StateLog", "StateEntry",
    "bulk_ingest", "load_sidecar", "scan_inputs",
    "PENDING", "IN_PROGRESS", "DONE", "FAILED", "SKIPPED",
]
