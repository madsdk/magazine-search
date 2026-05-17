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
