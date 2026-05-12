import json
from datetime import date
from pathlib import Path

from PIL import Image

from magsearch.ingest.ocr import FakeOCREngine, OCRRegion
from magsearch.ingest.pipeline import IngestPipeline, IngestOptions
from magsearch.manifest import Manifest
from tests.fixtures.pdfs import make_pdf


def _scripted_engine_for(num_pages: int) -> FakeOCREngine:
    return FakeOCREngine(
        responses=[
            [OCRRegion(text=f"text on page {i + 1}", bbox=(0, 0, 100, 20), confidence=1.0)]
            for i in range(num_pages)
        ]
    )


def test_pipeline_produces_complete_bundle(tmp_path: Path):
    src = make_pdf(tmp_path / "byte.pdf", num_pages=2)
    bundles = tmp_path / "bundles"
    engine = _scripted_engine_for(2)

    result = IngestPipeline(
        bundles_root=bundles,
        ocr_engine=engine,
        options=IngestOptions(
            title="Byte", issue="Vol 10 No 12",
            publication_date=date(1985, 12, 1), publisher="McGraw-Hill",
        ),
    ).run(src)

    bundle = bundles / result.id
    assert (bundle / "manifest.json").exists()
    assert (bundle / "original.pdf").exists()
    assert (bundle / "pages" / "0001.webp").exists()
    assert (bundle / "pages" / "0002.webp").exists()
    assert (bundle / "thumbs" / "0001.webp").exists()
    assert (bundle / "thumbs" / "0002.webp").exists()
    assert (bundle / "ocr" / "0001.json").exists()
    assert (bundle / "cover.webp").exists()

    manifest = Manifest.model_validate_json((bundle / "manifest.json").read_text())
    assert manifest.id == "byte-1985-12"
    assert manifest.page_count == 2
    assert manifest.original_format == "pdf"
    assert manifest.pages[0].text == "text on page 1"
    assert manifest.ocr_engine == "fake"
    # Every entry in checksums actually exists in the bundle.
    for c in manifest.checksums:
        assert (bundle / c.path).exists()


def test_pipeline_is_idempotent_without_force(tmp_path: Path):
    src = make_pdf(tmp_path / "byte.pdf", num_pages=1)
    bundles = tmp_path / "bundles"

    r1 = IngestPipeline(
        bundles_root=bundles, ocr_engine=_scripted_engine_for(1),
        options=IngestOptions(title="Byte", publication_date=date(1985, 12, 1)),
    ).run(src)
    mtime1 = (bundles / r1.id / "manifest.json").stat().st_mtime_ns

    r2 = IngestPipeline(
        bundles_root=bundles, ocr_engine=_scripted_engine_for(1),
        options=IngestOptions(title="Byte", publication_date=date(1985, 12, 1)),
    ).run(src)
    mtime2 = (bundles / r2.id / "manifest.json").stat().st_mtime_ns

    assert r1.id == r2.id
    assert mtime1 == mtime2  # manifest not rewritten


def test_pipeline_force_overwrites(tmp_path: Path):
    src = make_pdf(tmp_path / "byte.pdf", num_pages=1)
    bundles = tmp_path / "bundles"
    opts = IngestOptions(title="Byte", publication_date=date(1985, 12, 1))

    IngestPipeline(bundles, _scripted_engine_for(1), opts).run(src)
    pre = (bundles / "byte-1985-12" / "manifest.json").stat().st_mtime_ns

    IngestPipeline(bundles, _scripted_engine_for(1), opts).run(src, force=True)
    post = (bundles / "byte-1985-12" / "manifest.json").stat().st_mtime_ns

    assert post > pre


def test_pipeline_rejects_unknown_format(tmp_path: Path):
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"hello world")
    bundles = tmp_path / "bundles"
    import pytest
    with pytest.raises(ValueError, match="unrecognized format"):
        IngestPipeline(
            bundles, _scripted_engine_for(1), IngestOptions(title="x")
        ).run(junk)
