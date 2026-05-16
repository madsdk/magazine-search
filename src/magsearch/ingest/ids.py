import hashlib
import re
import unicodedata
from datetime import date
from pathlib import Path


_SLUG_KEEP = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    normalized = normalized.lower()
    normalized = _SLUG_KEEP.sub("-", normalized).strip("-")
    return normalized


def content_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def generate_id(
    *, title: str, publication_date: date | None, override: str | None, content_hash: str
) -> str:
    if override:
        return slugify(override)
    slug = slugify(title)
    if slug and publication_date is not None:
        return f"{slug}-{publication_date.year:04d}-{publication_date.month:02d}"
    return content_hash[:12]


def resolve_unique_id(
    *,
    base_id: str,
    content_hash: str,
    issue: str | None,
    bundles_root: Path,
) -> str:
    """Pick a bundle id that won't silently overwrite a different magazine.

    Returns `base_id` when its slot is free or already holds a manifest with a
    matching content_hash (so idempotent re-runs land on the existing bundle).
    On hash mismatch, tries successively longer suffixes — first the slugified
    issue (when non-empty), then content-hash prefixes of growing length — until
    a free or matching slot is found.
    """
    candidates = [base_id]
    issue_slug = slugify(issue) if issue else ""
    if issue_slug:
        candidates.append(f"{base_id}-{issue_slug}")
    for n in (6, 8, 12, 16):
        candidates.append(f"{base_id}-{content_hash[:n]}")

    from magsearch.manifest import Manifest  # local import: avoid circular

    for candidate in candidates:
        manifest_path = bundles_root / candidate / "manifest.json"
        if not manifest_path.exists():
            return candidate
        existing = Manifest.model_validate_json(manifest_path.read_text())
        if existing.content_hash == content_hash:
            return candidate
    raise RuntimeError(
        f"could not find a unique bundle id for {base_id!r} after "
        f"{len(candidates)} candidates — content_hash={content_hash[:12]}…"
    )
