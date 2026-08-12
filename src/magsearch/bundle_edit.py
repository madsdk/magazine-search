import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from magsearch import checksums
from magsearch.ingest.normalize import write_cover
from magsearch.manifest import Manifest, PageEntry
from magsearch.models import Magazine, Page

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


@dataclass
class StagedDrop:
    """A drop repair built in a sibling staging directory but not yet swapped
    in. The live bundle is untouched; `manifest` is already computed so a
    caller can update the database before committing the on-disk swap."""

    plan: DropPlan
    staging: Path
    backup: Path
    manifest: Manifest


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


def _fsync_file(path: Path) -> None:
    """fsync a single file's freshly written data before it is published by
    the directory rename, so a power loss can't leave it truncated."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir_best_effort(path: Path) -> None:
    """fsync a directory to persist its entries ahead of the swap. Not every
    platform supports fsync on a directory handle (notably Windows) — this is
    a durability nicety on top of the rename-based swap, not the correctness
    guarantee itself, so degrade gracefully rather than raising there.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def stage_drop(plan: DropPlan) -> StagedDrop:
    """Build the drop repair in a sibling staging directory. Returns a
    `StagedDrop` describing it. The live bundle is untouched by this call.

    Builds a sibling staging directory of hardlinks and fsyncs it, so the
    directory is ready to swap in with a single rename. Callers that decide
    not to proceed (e.g. a database write failed) must call `discard_staged`
    on the result rather than leaving it behind; `commit_drop` performs the
    swap.

    On any failure the staging directory is removed before the exception
    propagates, so a half-built staging tree is never left behind.
    """
    # plan_drop already proved bundle_dir is an immediate child of bundles_root.
    bundle = plan.bundle_dir
    root = plan.bundles_root
    staging = root / f"{bundle.name}{STAGING_SUFFIX}"
    backup = root / f"{bundle.name}{BACKUP_SUFFIX}"

    try:
        staging.mkdir()
        new_entries: list[PageEntry] = []
        # Every path any original page owns (dropped or surviving), plus the
        # cover and manifest, is accounted for explicitly below. Anything else
        # found walking the old bundle is an "extra" file — e.g. notes.txt —
        # that docs/datamodel/bundles.md permits as part of the format and
        # that must survive the repair untouched, not be silently destroyed.
        accounted = {"cover.webp", "manifest.json", "manifest.json.tmp"}
        for page in plan.manifest.pages:
            accounted.update((page.image_path, page.thumb_path, page.ocr_path))

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

        # Carry over every file the repair doesn't otherwise touch. This
        # generalizes what used to be a narrower `original.*`-only rule, so an
        # archive copy (whatever its extension) and any other declared or
        # undeclared extra file both survive at their original relative path.
        for path in bundle.rglob("*"):
            if not path.is_file():
                continue
            rel = str(path.relative_to(bundle))
            if rel in accounted:
                continue
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.hardlink_to(path)

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

        # cover.webp and manifest.json are the only newly written data blocks
        # in staging — everything else is a hardlink to an already-durable old
        # inode. fsync them and the directory entry before the swap so a power
        # loss can't leave a live bundle with a truncated manifest and no
        # backup left to recover from.
        if cover_rel:
            _fsync_file(staging / "cover.webp")
        _fsync_file(staging / "manifest.json")
        _fsync_dir_best_effort(staging)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    return StagedDrop(plan=plan, staging=staging, backup=backup, manifest=manifest)


def discard_staged(staged: StagedDrop) -> None:
    """Best-effort removal of a staging tree that will not be committed.

    Safe to call when the staging directory is already gone — e.g. after
    `commit_drop` has consumed it, or if `discard_staged` is called twice.
    """
    if staged.staging.exists():
        shutil.rmtree(staged.staging)


def commit_drop(staged: StagedDrop) -> Manifest:
    """Swap a staged repair into place. Returns the manifest now on disk.

    A two-step rename (`<id>` → `<id>.old`, then `<id>.new` → `<id>`) keeps the
    live bundle intact until the first rename and restores it from the backup
    if the second rename fails, so an interruption anywhere in the swap leaves
    a usable bundle. `rmtree(backup)` only runs after both renames succeed.
    """
    bundle = staged.plan.bundle_dir
    staging = staged.staging
    backup = staged.backup

    try:
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
    return staged.manifest


def apply_drop(plan: DropPlan) -> Manifest:
    """Repair the bundle on disk in one call. Returns the manifest now on disk.

    Equivalent to `commit_drop(stage_drop(plan))` — kept for callers that have
    no reason to do database work between staging and committing.
    """
    return commit_drop(stage_drop(plan))


def resync_magazine(session: Session, manifest: Manifest, count: int) -> bool:
    """Bring the DB rows in line with a bundle whose leading pages were dropped.

    Updates in place rather than delete-and-reimport so surviving Page.ids stay
    valid: research saves keep resolving, the pages_fts external-content index
    stays consistent through the pages_au trigger, and ingested_at is preserved.

    Returns False when the magazine has no row (nothing to do).
    """
    mag = session.get(Magazine, manifest.id)
    if mag is None:
        return False

    dropped = session.scalars(
        select(Page)
        .where(Page.magazine_id == manifest.id, Page.page_number <= count)
    ).all()
    for page in dropped:
        # Per-row ORM DELETE (not a bulk DELETE) so these objects leave the
        # session's identity map immediately — keeping in-session state
        # coherent with the database for the survivors this function goes on
        # to shift below.
        session.delete(page)
    session.flush()

    survivors = session.scalars(
        select(Page)
        .where(Page.magazine_id == manifest.id)
        .order_by(Page.page_number)
    ).all()

    # Two passes via the negative range. uq_pages_mag_page is checked per row
    # and SQLite gives no ordering guarantee, so a direct `n -= count` can
    # collide with a row that has not shifted yet. No live page_number is
    # negative, so the intermediate values are always free.
    for page in survivors:
        page.page_number = -page.page_number
    session.flush()
    for page in survivors:
        page.page_number = -page.page_number - count
    session.flush()

    by_number = {entry.page_number: entry for entry in manifest.pages}
    for page in survivors:
        entry = by_number[page.page_number]
        page.image_path = f"{manifest.id}/{entry.image_path}"
        page.thumb_path = f"{manifest.id}/{entry.thumb_path}"

    mag.page_count = len(manifest.pages)
    mag.cover_path = f"{manifest.id}/{manifest.cover_path}" if manifest.cover_path else ""
    session.flush()
    return True
