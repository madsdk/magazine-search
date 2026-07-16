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


def test_missing_file_not_double_reported_with_checksums(tmp_path):
    bundle, _, mag, pages = _stage(tmp_path)
    (bundle / "pages" / "0001.webp").unlink()
    report = check_bundle(bundle, "mag-1", mag, pages, verify_checksums=True)
    missing = [f for f in report.findings
               if f.message == "missing" and f.path == "pages/0001.webp"]
    assert len(missing) == 1, [(x.message, x.page, x.path) for x in report.findings]


def test_non_list_regions_is_error(tmp_path):
    bundle, _, mag, pages = _stage(tmp_path)
    (bundle / "ocr" / "0001.json").write_text(
        json.dumps({"width": 20, "height": 26, "regions": "oops"})
    )
    report = check_bundle(bundle, "mag-1", mag, pages)
    assert any(f.level == "error" and "OCR JSON shape" in f.message
               for f in report.findings)
