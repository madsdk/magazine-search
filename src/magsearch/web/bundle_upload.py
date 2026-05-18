"""Pure validation/staging logic for the web bundle upload endpoint.

`extract_and_stage` is the single entry point. It validates a zipped bundle,
extracts it to a temp staging directory under `bundles_dir`, verifies the
manifest's per-file checksums, and atomic-renames the staging directory into
its final location `bundles_dir/<id>/`. It does NOT touch the database; the
caller invokes `import_bundle()` after staging succeeds.

Invariants:
  - Atomic on success: the bundle either appears fully at bundles_dir/<id>/
    or not at all.
  - No residue on failure: temp staging directory is removed.
  - Idempotent: re-uploading the same content returns the existing path
    without re-extracting.
"""

from __future__ import annotations

import errno
import os
import shutil
import uuid
import zipfile
from pathlib import Path

from magsearch.ingest.ids import content_hash
from magsearch.manifest import Manifest


# Hard cap on the manifest.json entry, independent of the bundle-wide
# max_uncompressed_bytes setting. A legitimate manifest is well under this
# even for a thousand-page magazine, so a tightly compressed manifest entry
# that decompresses to hundreds of megabytes (a zip-bomb targeted at this
# one read) gets rejected on the header check before any decompression.
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024  # 64 MB


class BundleUploadError(Exception):
    """Raised when an uploaded zip is rejected. Message is safe to display."""


def extract_and_stage(
    zip_path: Path,
    bundles_dir: Path,
    *,
    max_uncompressed_bytes: int,
) -> Path:
    """Validate `zip_path` and stage it under `bundles_dir/<id>/`.

    Returns the staged bundle directory.
    """
    bundles_dir = Path(bundles_dir)
    bundles_dir.mkdir(parents=True, exist_ok=True)

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise BundleUploadError("file is not a valid zip archive")

    with zf:
        prefix = _resolve_bundle_prefix(zf)

        # Size caps fire BEFORE the manifest is read so a maliciously
        # compressed manifest entry (small on disk, huge after inflate)
        # can never force the process to allocate that much memory.
        total = sum(info.file_size for info in zf.infolist())
        if total > max_uncompressed_bytes:
            mb = max_uncompressed_bytes // (1024 * 1024)
            raise BundleUploadError(f"bundle would exceed max size of {mb} MB")

        manifest = _read_manifest(zf, prefix)

        # manifest.id is attacker-controlled. Require it to resolve to a
        # direct child of bundles_dir before using it in any filesystem path,
        # so that "../escape", "/etc/foo", "nested/sub", "." etc. can never
        # publish or rename a bundle outside the configured root.
        resolved_root = bundles_dir.resolve()
        try:
            resolved_target = (bundles_dir / manifest.id).resolve()
        except (ValueError, OSError):
            raise BundleUploadError(
                f"manifest id {manifest.id!r} is not a valid path component"
            )
        if resolved_target.parent != resolved_root or resolved_target == resolved_root:
            raise BundleUploadError(
                f"manifest id {manifest.id!r} must be a simple name under the bundles directory"
            )

        existing = bundles_dir / manifest.id
        if existing.exists():
            existing_manifest_path = existing / "manifest.json"
            if existing_manifest_path.exists():
                existing_manifest = Manifest.model_validate_json(
                    existing_manifest_path.read_text()
                )
                if existing_manifest.content_hash == manifest.content_hash:
                    return existing
            raise BundleUploadError(
                f"bundle id {manifest.id!r} already exists with different content"
            )

        staging = bundles_dir / f".upload-{manifest.id}-{uuid.uuid4().hex}"
        try:
            _extract_under_prefix(zf, prefix, staging)
            _verify_checksums(staging, manifest)
            final = bundles_dir / manifest.id
            try:
                os.rename(staging, final)
            except OSError as exc:
                if exc.errno == errno.EXDEV:
                    raise BundleUploadError(
                        "cannot publish bundle: temp dir and bundles_dir are on different filesystems"
                    ) from exc
                raise
            return final
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise


def _resolve_bundle_prefix(zf: zipfile.ZipFile) -> str:
    """Locate the in-zip path that holds manifest.json.

    Returns either "" (manifest at root) or "<dir>/" (single top-level folder).
    Raises BundleUploadError otherwise.
    """
    names = [n for n in zf.namelist() if not n.endswith("/")]
    if "manifest.json" in names:
        return ""
    top_dirs = {n.split("/", 1)[0] for n in names if "/" in n}
    if len(top_dirs) == 1:
        only = next(iter(top_dirs))
        if f"{only}/manifest.json" in names:
            return f"{only}/"
    raise BundleUploadError(
        "manifest.json not found at zip root or in a single top-level folder"
    )


def _read_manifest(zf: zipfile.ZipFile, prefix: str) -> Manifest:
    name = f"{prefix}manifest.json"
    try:
        info = zf.getinfo(name)
    except KeyError:
        raise BundleUploadError("manifest.json missing from zip")

    # Reject on the declared size first — cheap, no decompression.
    if info.file_size > _MAX_MANIFEST_BYTES:
        raise BundleUploadError(
            f"manifest.json is larger than the allowed {_MAX_MANIFEST_BYTES // (1024 * 1024)} MB"
        )

    # Then stream-read with the same hard cap so a lying header (declared
    # small, actually huge) can't sneak past either.
    buf = bytearray()
    with zf.open(info) as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > _MAX_MANIFEST_BYTES:
                raise BundleUploadError(
                    f"manifest.json is larger than the allowed {_MAX_MANIFEST_BYTES // (1024 * 1024)} MB"
                )

    try:
        return Manifest.model_validate_json(bytes(buf))
    except Exception as exc:  # pydantic ValidationError or json error
        raise BundleUploadError(f"manifest.json is invalid: {exc}")


def _extract_under_prefix(zf: zipfile.ZipFile, prefix: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=False)
    dest_resolved = dest.resolve()
    for info in zf.infolist():
        name = info.filename
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix):]
        if not rel or rel.endswith("/"):
            continue
        target = (dest / rel).resolve()
        try:
            target.relative_to(dest_resolved)
        except ValueError:
            raise BundleUploadError(f"zip contains unsafe path: {name!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, target.open("wb") as out:
            shutil.copyfileobj(src, out)


def _verify_checksums(bundle_dir: Path, manifest: Manifest) -> None:
    for c in manifest.checksums:
        path = bundle_dir / c.path
        if not path.exists():
            raise BundleUploadError(f"file listed in manifest is missing: {c.path}")
        if content_hash(path) != c.sha256:
            raise BundleUploadError(f"checksum mismatch: {c.path}")
