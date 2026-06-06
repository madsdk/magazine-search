import json
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from PIL import Image

from magsearch.ingest.formats import detect_format, page_count, read_pages
from magsearch.ingest.ids import content_hash, generate_id, resolve_unique_id
from magsearch.ingest.normalize import encode_page, encode_thumb, write_cover
from magsearch.ingest.ocr import OCREngine, OCRRegion, concatenate_reading_order
from magsearch.manifest import FileChecksum, Manifest, PageEntry


@dataclass
class IngestOptions:
    title: str = ""
    issue: str | None = None
    publication_date: date | None = None
    publisher: str | None = None
    id_override: str | None = None


@dataclass
class IngestResult:
    id: str
    bundle_dir: Path
    manifest: Manifest


class IngestPipeline:
    def __init__(
        self,
        bundles_root: Path,
        ocr_engine: OCREngine,
        options: IngestOptions,
    ) -> None:
        self.bundles_root = bundles_root
        self.engine = ocr_engine
        self.options = options

    def run(
        self,
        source: Path,
        force: bool = False,
        on_page: Callable[[int, int], None] | None = None,
    ) -> IngestResult:
        fmt = detect_format(source)
        if fmt is None:
            raise ValueError(f"unrecognized format for {source}")
        total_pages = page_count(source, fmt)

        hash_hex = content_hash(source)
        magazine_id = generate_id(
            title=self.options.title,
            publication_date=self.options.publication_date,
            override=self.options.id_override,
            content_hash=hash_hex,
        )
        # An explicit --id is an instruction, not a suggestion: don't disambiguate.
        # The importer will surface the collision so the user can decide what to do.
        if self.options.id_override is None:
            magazine_id = resolve_unique_id(
                base_id=magazine_id,
                content_hash=hash_hex,
                issue=self.options.issue,
                bundles_root=self.bundles_root,
            )
        bundle = self.bundles_root / magazine_id

        manifest_path = bundle / "manifest.json"
        if not force and manifest_path.exists():
            existing = Manifest.model_validate_json(manifest_path.read_text())
            if existing.content_hash == hash_hex:
                return IngestResult(id=magazine_id, bundle_dir=bundle, manifest=existing)

        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "pages").mkdir(exist_ok=True)
        (bundle / "thumbs").mkdir(exist_ok=True)
        (bundle / "ocr").mkdir(exist_ok=True)

        original_dest = bundle / f"original.{fmt}"
        if not original_dest.exists() or force:
            shutil.copyfile(source, original_dest)

        page_entries: list[PageEntry] = []
        for page_num, image in read_pages(source, fmt):
            stem = f"{page_num:04d}"
            page_img = bundle / "pages" / f"{stem}.webp"
            thumb_img = bundle / "thumbs" / f"{stem}.webp"
            ocr_json = bundle / "ocr" / f"{stem}.json"

            encode_page(image, page_img)
            encode_thumb(image, thumb_img)

            try:
                regions: list[OCRRegion] = self.engine.recognize(image)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "OCR failed on page %d of %s: %s — recording empty text",
                    page_num, source.name, exc,
                )
                regions = []
            # OCR runs on the full-resolution source image; the page image
            # served to the browser is downscaled by encode_page. Rescale
            # bboxes into displayed-image pixel coordinates so the page
            # viewer's highlight overlay can use them directly. Stored as
            # {"width", "height", "regions": [...]} — the dict shape marks
            # the file as already in displayed-image coords, which keeps
            # the migration command's "needs rescaling?" check unambiguous.
            with Image.open(page_img) as _disp:
                disp_w, disp_h = _disp.size
            src_w, src_h = image.size
            sx = disp_w / src_w if src_w else 1.0
            sy = disp_h / src_h if src_h else 1.0
            ocr_json.write_text(json.dumps({
                "width": disp_w,
                "height": disp_h,
                "regions": [
                    {
                        "text": r.text,
                        "bbox": [r.bbox[0] * sx, r.bbox[1] * sy, r.bbox[2] * sx, r.bbox[3] * sy],
                        "confidence": r.confidence,
                    }
                    for r in regions
                ],
            }))
            page_text = concatenate_reading_order(regions)

            page_entries.append(PageEntry(
                page_number=page_num,
                image_path=str(page_img.relative_to(bundle)),
                thumb_path=str(thumb_img.relative_to(bundle)),
                ocr_path=str(ocr_json.relative_to(bundle)),
                text=page_text,
            ))

            if on_page is not None:
                on_page(page_num, total_pages)

        # cover = first thumbnail
        first_thumb = bundle / "thumbs" / "0001.webp"
        cover = bundle / "cover.webp"
        if first_thumb.exists():
            write_cover(first_thumb, cover)
        cover_rel = "cover.webp" if cover.exists() else ""

        manifest = Manifest(
            schema_version=1,
            id=magazine_id,
            title=self.options.title or source.stem,
            issue=self.options.issue,
            publication_date=self.options.publication_date,
            publisher=self.options.publisher,
            original_filename=source.name,
            original_format=fmt,
            page_count=len(page_entries),
            content_hash=hash_hex,
            ocr_engine=self.engine.name,
            ocr_engine_version=self.engine.version,
            cover_path=cover_rel,
            pages=page_entries,
            checksums=_collect_checksums(bundle),
        )

        # atomic write of manifest
        tmp_manifest = bundle / "manifest.json.tmp"
        tmp_manifest.write_text(manifest.model_dump_json(indent=2))
        tmp_manifest.replace(manifest_path)

        return IngestResult(id=magazine_id, bundle_dir=bundle, manifest=manifest)


def _collect_checksums(bundle: Path) -> list[FileChecksum]:
    out: list[FileChecksum] = []
    for p in sorted(bundle.rglob("*")):
        if p.is_file() and p.name not in ("manifest.json", "manifest.json.tmp"):
            out.append(FileChecksum(path=str(p.relative_to(bundle)), sha256=content_hash(p)))
    return out
