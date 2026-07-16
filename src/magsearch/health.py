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
        err_msg = "manifest.json missing (bundle directory absent or incomplete)"
        findings.append(Finding("error", err_msg))
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
    missing_paths: set[str] = set()
    image_files: list[tuple[int | None, str]] = []  # (page or None, rel path) to decode
    for entry in manifest.pages:
        for rel in (entry.image_path, entry.thumb_path, entry.ocr_path):
            if not (bundle_dir / rel).exists():
                findings.append(Finding("error", "missing", page=entry.page_number, path=rel))
                missing_paths.add(rel)
        image_files.append((entry.page_number, entry.image_path))
        image_files.append((entry.page_number, entry.thumb_path))
    original_rel = f"original.{manifest.original_format}"
    if not (bundle_dir / original_rel).exists():
        findings.append(Finding("error", "missing", path=original_rel))
        missing_paths.add(original_rel)
    if manifest.cover_path:
        if not (bundle_dir / manifest.cover_path).exists():
            findings.append(Finding("error", "missing", path=manifest.cover_path))
            missing_paths.add(manifest.cover_path)
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
                if c.path not in missing_paths:
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
            if not isinstance(regions, list):
                findings.append(Finding("error", "unrecognized OCR JSON shape",
                                        page=entry.page_number, path=entry.ocr_path))
                continue
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
