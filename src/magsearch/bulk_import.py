"""Bulk import: walk a directory of already-ingested bundles and import each
into the database.

Thin loop over `import_bundle()`. Manifest-driven, so no metadata flags. Per-
bundle state log mirrors the bulk-ingest one so server-side imports of huge
collections are also resumable.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from magsearch.db import session_scope
from magsearch.importer import import_bundle

_LOG = logging.getLogger(__name__)

PENDING = "pending"
IN_PROGRESS = "in_progress"
DONE = "done"
FAILED = "failed"


@dataclass
class ImportStateEntry:
    bundle: str
    status: str = PENDING
    attempts: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    magazine_id: str | None = None
    error: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ImportStateLog:
    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, ImportStateEntry] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                self.entries[raw["bundle"]] = ImportStateEntry(**raw)

    def get(self, bundle: str) -> ImportStateEntry:
        return self.entries.setdefault(bundle, ImportStateEntry(bundle=bundle))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        lines = [json.dumps(asdict(e)) for e in self.entries.values()]
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""))
        tmp.replace(self.path)


@dataclass
class BulkImportResult:
    processed: int
    succeeded: int
    failed: int
    skipped: int
    state_file: Path
    magazine_ids: list[str] = field(default_factory=list)


def discover_bundles(bundles_dir: Path) -> list[Path]:
    """List immediate subdirs of `bundles_dir` that contain a manifest.json."""
    if not bundles_dir.is_dir():
        raise FileNotFoundError(f"bundles dir not found: {bundles_dir}")
    return sorted(
        p for p in bundles_dir.iterdir()
        if p.is_dir() and (p / "manifest.json").exists()
    )


def bulk_import(
    *,
    bundles_dir: Path,
    state_path: Path | None,
    session_factory: sessionmaker,
    retry_failed: bool = False,
    halt_on_error: bool = False,
    on_bundle_start: Callable[[int, int, Path], None] = lambda i, n, p: None,
    on_bundle_end: Callable[[int, int, Path, ImportStateEntry], None] = lambda i, n, p, e: None,
    on_warning: Callable[[str], None] = lambda msg: None,
) -> BulkImportResult:
    """Import every bundle subdirectory under `bundles_dir`, persisting state."""
    bundles = discover_bundles(bundles_dir)
    state_path = state_path or (bundles_dir / ".bulk-import-state.jsonl")
    state = ImportStateLog(state_path)

    to_process: list[Path] = []
    to_skip: list[Path] = []
    for b in bundles:
        entry = state.get(b.name)
        if entry.status == DONE:
            to_skip.append(b)
        elif entry.status == FAILED and not retry_failed:
            to_skip.append(b)
        else:
            to_process.append(b)

    magazine_ids: list[str] = []
    succeeded = failed = 0
    total = len(to_process) + len(to_skip)

    for idx, b in enumerate(to_skip, start=1):
        on_bundle_start(idx, total, b)
        on_bundle_end(idx, total, b, state.get(b.name))

    for offset, b in enumerate(to_process, start=1):
        idx = len(to_skip) + offset
        entry = state.get(b.name)
        entry.attempts += 1
        entry.status = IN_PROGRESS
        entry.started_at = _now_iso()
        entry.error = None
        state.save()
        on_bundle_start(idx, total, b)
        try:
            with session_scope(session_factory) as s:
                mag_id = import_bundle(b, s)
            entry.status = DONE
            entry.magazine_id = mag_id
            entry.error = None
            magazine_ids.append(mag_id)
            succeeded += 1
        except Exception as exc:  # noqa: BLE001 — bulk runs must record any failure
            entry.status = FAILED
            entry.error = f"{type(exc).__name__}: {exc}"
            failed += 1
            _LOG.exception("import failed for %s", b)
            entry.finished_at = _now_iso()
            state.save()
            on_bundle_end(idx, total, b, entry)
            if halt_on_error:
                break
            continue
        entry.finished_at = _now_iso()
        state.save()
        on_bundle_end(idx, total, b, entry)

    return BulkImportResult(
        processed=succeeded + failed,
        succeeded=succeeded,
        failed=failed,
        skipped=len(to_skip),
        state_file=state_path,
        magazine_ids=magazine_ids,
    )


__all__ = [
    "BulkImportResult", "ImportStateEntry", "ImportStateLog",
    "bulk_import", "discover_bundles",
    "PENDING", "IN_PROGRESS", "DONE", "FAILED",
]
