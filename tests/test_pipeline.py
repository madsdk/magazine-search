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


def _make_large_pdf(path: Path, page_pt: tuple[int, int] = (650, 950)) -> Path:
    """A PDF whose rendered-at-200dpi long edge exceeds PAGE_LONG_EDGE (1800),
    so encode_page actually downscales the displayed image. Required to exercise
    the bbox rescaling path — small fixture PDFs render below 1800 px and would
    be passed through unchanged, masking the scale factor."""
    import fitz
    doc = fitz.open()
    for _ in range(1):
        doc.new_page(width=page_pt[0], height=page_pt[1])
    doc.save(str(path))
    doc.close()
    return path


def test_pipeline_ocr_json_is_dict_with_rescaled_bboxes(tmp_path: Path):
    src = _make_large_pdf(tmp_path / "big.pdf")
    bundles = tmp_path / "bundles"
    # A region whose bbox extends near the right/bottom edge of the source image
    # so the rescaled coordinates differ visibly from the input.
    src_region = OCRRegion(text="hello", bbox=(100.0, 200.0, 1500.0, 2400.0), confidence=0.9)
    engine = FakeOCREngine(responses=[[src_region]])

    result = IngestPipeline(
        bundles_root=bundles, ocr_engine=engine,
        options=IngestOptions(title="Big", publication_date=date(1985, 12, 1)),
    ).run(src)

    bundle = bundles / result.id
    doc = json.loads((bundle / "ocr" / "0001.json").read_text())

    # (a) New self-describing schema.
    assert isinstance(doc, dict), "OCR JSON must be a dict, not a flat array"
    assert set(doc) == {"width", "height", "regions"}
    assert isinstance(doc["regions"], list)
    assert doc["width"] > 0 and doc["height"] > 0

    # The embedded width/height equal the displayed page image's natural dims.
    with Image.open(bundle / "pages" / "0001.webp") as disp:
        disp_w, disp_h = disp.size
    assert doc["width"] == disp_w
    assert doc["height"] == disp_h

    # (b) Bbox scaling matches encode_page's resize. The OCR engine "saw" the
    # full-resolution rendered page; the bundle's displayed page is downscaled.
    # Reconstruct the same source image the engine received to recover src dims.
    from magsearch.ingest.formats import detect_format, read_pages
    fmt = detect_format(src)
    src_w, src_h = next(read_pages(src, fmt))[1].size
    sx = disp_w / src_w
    sy = disp_h / src_h
    # Sanity-check the scenario: the displayed image must actually be smaller,
    # otherwise the test would pass trivially with sx = sy = 1.
    assert sx < 0.95 and sy < 0.95, f"expected real downscaling, got sx={sx}, sy={sy}"

    (rx0, ry0, rx1, ry1) = doc["regions"][0]["bbox"]
    assert rx0 == src_region.bbox[0] * sx
    assert ry0 == src_region.bbox[1] * sy
    assert rx1 == src_region.bbox[2] * sx
    assert ry1 == src_region.bbox[3] * sy

    # Resulting bbox stays inside the displayed-image coordinate space.
    assert 0 <= rx0 < rx1 <= disp_w
    assert 0 <= ry0 < ry1 <= disp_h


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


def test_pipeline_disambiguates_same_month_collision(tmp_path: Path):
    """Two different files with the same title+date must produce two bundles."""
    bundles = tmp_path / "bundles"
    src_a = make_pdf(tmp_path / "a.pdf", num_pages=1, page_text=["alpha"])
    src_b = make_pdf(tmp_path / "b.pdf", num_pages=2, page_text=["beta-one", "beta-two"])
    opts = IngestOptions(title="Byte", publication_date=date(1985, 12, 1))

    r1 = IngestPipeline(bundles, _scripted_engine_for(1), opts).run(src_a)
    r2 = IngestPipeline(bundles, _scripted_engine_for(2), opts).run(src_b)

    assert r1.id == "byte-1985-12"
    assert r2.id != r1.id
    assert r2.id.startswith("byte-1985-12-")
    # Both bundles exist on disk with their own manifests.
    assert (bundles / r1.id / "manifest.json").exists()
    assert (bundles / r2.id / "manifest.json").exists()
    # The first bundle wasn't overwritten — its page count is still 1.
    m1 = Manifest.model_validate_json((bundles / r1.id / "manifest.json").read_text())
    assert m1.page_count == 1


def test_pipeline_collision_prefers_issue_slug(tmp_path: Path):
    bundles = tmp_path / "bundles"
    src_a = make_pdf(tmp_path / "a.pdf", num_pages=1, page_text=["alpha"])
    src_b = make_pdf(tmp_path / "b.pdf", num_pages=1, page_text=["beta"])

    IngestPipeline(
        bundles, _scripted_engine_for(1),
        IngestOptions(title="Byte", publication_date=date(1985, 12, 1)),
    ).run(src_a)
    r2 = IngestPipeline(
        bundles, _scripted_engine_for(1),
        IngestOptions(
            title="Byte", publication_date=date(1985, 12, 1),
            issue="Christmas Double Issue",
        ),
    ).run(src_b)

    assert r2.id == "byte-1985-12-christmas-double-issue"


def test_pipeline_collision_idempotent_re_run(tmp_path: Path):
    """Re-running the second-collision file lands on the same disambiguated id."""
    bundles = tmp_path / "bundles"
    src_a = make_pdf(tmp_path / "a.pdf", num_pages=1, page_text=["alpha"])
    src_b = make_pdf(tmp_path / "b.pdf", num_pages=1, page_text=["beta"])
    opts = IngestOptions(title="Byte", publication_date=date(1985, 12, 1))

    IngestPipeline(bundles, _scripted_engine_for(1), opts).run(src_a)
    r2_first = IngestPipeline(bundles, _scripted_engine_for(1), opts).run(src_b)
    r2_second = IngestPipeline(bundles, _scripted_engine_for(1), opts).run(src_b)

    assert r2_first.id == r2_second.id
    # Manifest wasn't rewritten on the second call (idempotency held).
    mtime_first = (bundles / r2_first.id / "manifest.json").stat().st_mtime_ns
    r2_third = IngestPipeline(bundles, _scripted_engine_for(1), opts).run(src_b)
    mtime_third = (bundles / r2_third.id / "manifest.json").stat().st_mtime_ns
    assert mtime_first == mtime_third
