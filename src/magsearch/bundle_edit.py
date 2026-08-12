import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from magsearch import checksums
from magsearch.ingest.normalize import write_cover
from magsearch.manifest import Manifest, PageEntry

STAGING_SUFFIX = ".new"
BACKUP_SUFFIX = ".old"

_WHITESPACE = re.compile(r"\s+")


class BundleEditError(Exception):
    """A bundle cannot be edited safely. The caller should skip it."""


@dataclass
class DropPlan:
    bundles_root: Path
    bundle_dir: Path
    magazine_id: str
    count: int
    manifest: Manifest
    dropped: list[PageEntry]
    surviving: list[PageEntry]
    new_page_count: int


def page_text_preview(entry: PageEntry, width: int = 80) -> str:
    """One-line preview of a page's OCR text, for the operator to eyeball.

    An empty result is meaningful — a logo or credits sheet often OCRs to
    nothing — so it is labelled rather than shown as an empty string.
    """
    text = _WHITESPACE.sub(" ", entry.text).strip()
    if not text:
        return "(no text)"
    if len(text) <= width:
        return text
    return text[:width].rsplit(" ", 1)[0].rstrip() + "…"


def plan_drop(bundles_root: Path, magazine_id: str, count: int) -> DropPlan:
    """Validate a leading-page drop and describe it. Reads only; writes nothing.

    Raises BundleEditError for every condition that makes the repair unsafe, so
    callers can report and skip the bundle without a half-applied edit.
    """
    if count < 1:
        raise BundleEditError(f"count must be at least 1 (got {count})")

    # An id is a single directory name, never a path. Checked before touching
    # the filesystem so a traversing or absolute id can never name a target
    # outside the root — the same guard importer.delete_bundle_dir applies.
    if magazine_id in ("", ".", "..") or magazine_id != Path(magazine_id).name:
        raise BundleEditError(f"refusing to edit {magazine_id!r}: not a bundle directory name")

    root = Path(bundles_root).resolve()
    bundle_dir = (root / magazine_id).resolve()
    if bundle_dir.parent != root:
        raise BundleEditError(f"refusing to edit {bundle_dir}: not a bundle directory")

    for suffix in (STAGING_SUFFIX, BACKUP_SUFFIX):
        stray = bundle_dir.parent / f"{bundle_dir.name}{suffix}"
        if stray.exists():
            raise BundleEditError(
                f"{stray.name} exists — left over from an interrupted run; "
                f"inspect and remove it before retrying"
            )

    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.exists():
        raise BundleEditError(f"manifest.json missing in {bundle_dir}")
    try:
        manifest = Manifest.model_validate_json(manifest_path.read_text())
    except Exception as exc:
        raise BundleEditError(f"manifest.json unparseable: {exc}") from exc

    pages = sorted(manifest.pages, key=lambda p: p.page_number)
    total = len(pages)
    # Renumbering subtracts a constant, so it is only correct when the existing
    # numbering is exactly 1..N with no gaps or duplicates.
    if [p.page_number for p in pages] != list(range(1, total + 1)):
        raise BundleEditError(f"page numbers are not 1..{total} — refusing to renumber")
    if count >= total:
        raise BundleEditError(
            f"dropping {count} of {total} pages would leave 0 pages — "
            f"use `magsearch delete` to remove a magazine"
        )

    # Verify before editing: repairing an already-damaged bundle would write a
    # fresh manifest that certifies the damage as correct.
    try:
        checksums.verify(bundle_dir, manifest)
    except checksums.ChecksumError as exc:
        raise BundleEditError(str(exc)) from exc

    return DropPlan(
        bundles_root=root,
        bundle_dir=bundle_dir,
        magazine_id=manifest.id,
        count=count,
        manifest=manifest,
        dropped=pages[:count],
        surviving=pages[count:],
        new_page_count=total - count,
    )


def _renumbered(rel_path: str, new_number: int) -> str:
    """Rebuild a bundle-relative path with a new NNNN stem, keeping the
    producer's own directory and suffix (paths need not be pages/NNNN.webp)."""
    p = Path(rel_path)
    return str(p.with_name(f"{new_number:04d}{p.suffix}"))


def apply_drop(plan: DropPlan) -> Manifest:
    """Repair the bundle on disk. Returns the manifest now on disk.

    Builds a sibling staging directory of hardlinks and swaps it in, so the live
    bundle is untouched until a single rename, and an interruption anywhere
    leaves it intact.
    """
    # plan_drop already proved bundle_dir is an immediate child of bundles_root.
    bundle = plan.bundle_dir
    root = plan.bundles_root
    staging = root / f"{bundle.name}{STAGING_SUFFIX}"
    backup = root / f"{bundle.name}{BACKUP_SUFFIX}"

    try:
        staging.mkdir()
        new_entries: list[PageEntry] = []
        for new_number, entry in enumerate(plan.surviving, start=1):
            new_entry = PageEntry(
                page_number=new_number,
                image_path=_renumbered(entry.image_path, new_number),
                thumb_path=_renumbered(entry.thumb_path, new_number),
                ocr_path=_renumbered(entry.ocr_path, new_number),
                text=entry.text,
            )
            for old_rel, new_rel in (
                (entry.image_path, new_entry.image_path),
                (entry.thumb_path, new_entry.thumb_path),
                (entry.ocr_path, new_entry.ocr_path),
            ):
                dest = staging / new_rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Hardlink, not copy: staging a 60 MB+ bundle must not cost
                # disk or time, and these files are never written through.
                dest.hardlink_to(bundle / old_rel)
            new_entries.append(new_entry)

        for original in bundle.glob("original.*"):
            (staging / original.name).hardlink_to(original)

        # The old cover is a copy of the junk thumbnail — rebuild, never link.
        cover_rel = ""
        if new_entries:
            write_cover(staging / new_entries[0].thumb_path, staging / "cover.webp")
            cover_rel = "cover.webp"

        manifest = plan.manifest.model_copy(update={
            "page_count": len(new_entries),
            "pages": new_entries,
            "cover_path": cover_rel,
            "checksums": checksums.collect(staging),
        })
        tmp_manifest = staging / "manifest.json.tmp"
        tmp_manifest.write_text(manifest.model_dump_json(indent=2))
        tmp_manifest.replace(staging / "manifest.json")

        bundle.rename(backup)
        try:
            staging.rename(bundle)
        except OSError:
            backup.rename(bundle)  # put the live bundle back before re-raising
            raise
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    shutil.rmtree(backup)
    return manifest
