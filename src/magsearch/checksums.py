from pathlib import Path

from magsearch.ingest.ids import content_hash
from magsearch.manifest import FileChecksum, Manifest

# manifest.json cannot checksum itself, and manifest.json.tmp is the transient
# file the atomic write in pipeline/bundle_edit renames into place.
_EXCLUDED = ("manifest.json", "manifest.json.tmp")


class ChecksumError(Exception):
    """A bundle's files do not match the checksums its manifest declares."""


def collect(bundle_dir: Path) -> list[FileChecksum]:
    """SHA-256 every file under `bundle_dir`, sorted by path for determinism."""
    out: list[FileChecksum] = []
    for p in sorted(bundle_dir.rglob("*")):
        if p.is_file() and p.name not in _EXCLUDED:
            out.append(FileChecksum(path=str(p.relative_to(bundle_dir)), sha256=content_hash(p)))
    return out


def verify(bundle_dir: Path, manifest: Manifest) -> None:
    """Raise ChecksumError unless every file the manifest declares is present and intact."""
    for c in manifest.checksums:
        path = bundle_dir / c.path
        if not path.exists():
            raise ChecksumError(f"bundle missing file {c.path}")
        if content_hash(path) != c.sha256:
            raise ChecksumError(f"checksum mismatch on {c.path}")
