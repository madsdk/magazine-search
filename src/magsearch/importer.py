import html
import shutil
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from magsearch.ingest.ids import content_hash
from magsearch.manifest import Manifest
from magsearch.models import Magazine, Page


class ImportError(Exception):
    """Raised when a bundle cannot be imported safely."""


def delete_bundle_dir(bundles_root: Path, magazine_id: str) -> None:
    """Delete the on-disk bundle for `magazine_id`, with a path-traversal guard.

    Safe to call when the directory does not exist.
    """
    root = Path(bundles_root).resolve()
    target = (root / magazine_id).resolve()
    # Guard: target must be a proper child of root.
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(f"refusing to delete outside bundles root: {target}")
    if target == root:
        raise ValueError("refusing to delete the bundles root itself")
    if target.exists():
        shutil.rmtree(target)


def import_bundle(bundle_dir: Path, session: Session) -> str:
    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.exists():
        raise ImportError(f"manifest.json missing in {bundle_dir}")
    manifest = Manifest.model_validate_json(manifest_path.read_text())

    _verify_checksums(bundle_dir, manifest)

    existing = session.scalar(select(Magazine).where(Magazine.id == manifest.id))
    if existing is not None:
        if existing.content_hash != manifest.content_hash:
            raise ImportError(
                f"id collision: '{manifest.id}' already exists with content_hash "
                f"{existing.content_hash!r} but bundle has {manifest.content_hash!r}. "
                f"Use --id to disambiguate."
            )
        return manifest.id

    session.add(Magazine(
        id=manifest.id,
        title=manifest.title,
        issue=manifest.issue,
        publication_date=manifest.publication_date,
        publisher=manifest.publisher,
        original_filename=manifest.original_filename,
        original_format=manifest.original_format,
        page_count=manifest.page_count,
        content_hash=manifest.content_hash,
        cover_path=f"{manifest.id}/{manifest.cover_path}" if manifest.cover_path else "",
        ocr_engine=manifest.ocr_engine,
        ocr_engine_version=manifest.ocr_engine_version,
        ingested_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))
    session.flush()

    for entry in manifest.pages:
        session.add(Page(
            magazine_id=manifest.id,
            page_number=entry.page_number,
            image_path=f"{manifest.id}/{entry.image_path}",
            thumb_path=f"{manifest.id}/{entry.thumb_path}",
            text=html.escape(entry.text),
        ))
    return manifest.id


def _verify_checksums(bundle_dir: Path, manifest: Manifest) -> None:
    for c in manifest.checksums:
        path = bundle_dir / c.path
        if not path.exists():
            raise ImportError(f"bundle missing file {c.path}")
        if content_hash(path) != c.sha256:
            raise ImportError(f"checksum mismatch on {c.path}")
