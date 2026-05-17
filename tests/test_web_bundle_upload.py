"""Tests for src/magsearch/web/bundle_upload.py.

Pure-logic tests (this file) drive `extract_and_stage` directly with hand-rolled
zip files. Route-level tests come later via FastAPI's TestClient.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.models import Magazine
from magsearch.settings import get_settings
from magsearch.web.bundle_upload import BundleUploadError, extract_and_stage
from tests.fixtures.bundles import make_bundle, zip_bundle


_2_GB = 2 * 1024 * 1024 * 1024


def test_extract_and_stage_shape_a_publishes_bundle(tmp_path):
    src = make_bundle(tmp_path / "src")
    bundle_id = src.name
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    final_root = tmp_path / "final"
    final_root.mkdir()

    staged = extract_and_stage(
        zip_path, final_root, max_uncompressed_bytes=_2_GB,
    )

    assert staged == final_root / bundle_id
    assert (staged / "manifest.json").exists()
    # original.* and the page assets came through:
    assert any(staged.glob("original.*"))
    assert (staged / "pages").is_dir()
    assert (staged / "thumbs").is_dir()
    # No residue temp dir left behind.
    leftovers = [p.name for p in final_root.iterdir() if p.name.startswith(".upload-")]
    assert leftovers == []


def test_extract_and_stage_shape_b_publishes_bundle(tmp_path):
    src = make_bundle(tmp_path / "src")
    bundle_id = src.name
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="B")

    final_root = tmp_path / "final"
    final_root.mkdir()

    staged = extract_and_stage(
        zip_path, final_root, max_uncompressed_bytes=_2_GB,
    )

    assert staged == final_root / bundle_id
    assert (staged / "manifest.json").exists()


def test_corrupt_zip_is_rejected(tmp_path):
    bad = tmp_path / "not-a-zip.zip"
    bad.write_bytes(b"this is not a zip file")
    final_root = tmp_path / "final"
    final_root.mkdir()
    with pytest.raises(BundleUploadError, match="not a valid zip"):
        extract_and_stage(bad, final_root, max_uncompressed_bytes=_2_GB)
    assert list(final_root.iterdir()) == []


def test_manifest_missing_is_rejected(tmp_path):
    z = tmp_path / "no-manifest.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("something/else.txt", "hi")
    final_root = tmp_path / "final"
    final_root.mkdir()
    with pytest.raises(BundleUploadError, match="manifest.json not found"):
        extract_and_stage(z, final_root, max_uncompressed_bytes=_2_GB)
    assert list(final_root.iterdir()) == []


def test_invalid_manifest_is_rejected(tmp_path):
    z = tmp_path / "bad-manifest.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("manifest.json", "{not valid json")
    final_root = tmp_path / "final"
    final_root.mkdir()
    with pytest.raises(BundleUploadError, match="manifest.json is invalid"):
        extract_and_stage(z, final_root, max_uncompressed_bytes=_2_GB)
    assert list(final_root.iterdir()) == []


def test_size_limit_is_enforced(tmp_path):
    src = make_bundle(tmp_path / "src")
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")
    final_root = tmp_path / "final"
    final_root.mkdir()
    with pytest.raises(BundleUploadError, match="exceed max size"):
        extract_and_stage(zip_path, final_root, max_uncompressed_bytes=10)  # absurdly small
    assert list(final_root.iterdir()) == []


def test_path_traversal_is_rejected(tmp_path):
    # Build a valid bundle to grab a real manifest from, then add a malicious entry.
    src = make_bundle(tmp_path / "src")
    real_manifest = (src / "manifest.json").read_bytes()
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("manifest.json", real_manifest)
        zf.writestr("../escape.txt", "pwned")
    final_root = tmp_path / "final"
    final_root.mkdir()
    with pytest.raises(BundleUploadError, match="unsafe path"):
        extract_and_stage(z, final_root, max_uncompressed_bytes=_2_GB)
    assert list(final_root.iterdir()) == []


def test_idempotent_reupload_returns_existing_path(tmp_path):
    src = make_bundle(tmp_path / "src")
    bundle_id = src.name
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    final_root = tmp_path / "final"
    final_root.mkdir()

    # First upload publishes the bundle.
    first = extract_and_stage(zip_path, final_root, max_uncompressed_bytes=_2_GB)
    assert first == final_root / bundle_id
    first_mtime = (first / "manifest.json").stat().st_mtime_ns

    # Second upload with identical content short-circuits.
    second = extract_and_stage(zip_path, final_root, max_uncompressed_bytes=_2_GB)
    assert second == first
    # The on-disk manifest mtime is unchanged — i.e. nothing got re-extracted.
    assert (second / "manifest.json").stat().st_mtime_ns == first_mtime
    # No leftover .upload-* staging directories.
    leftovers = [p.name for p in final_root.iterdir() if p.name.startswith(".upload-")]
    assert leftovers == []


def test_id_collision_with_different_content_is_rejected(tmp_path):
    # Build two bundles that we'll force to share the same id.
    src_a = make_bundle(tmp_path / "src_a", num_pages=1)
    src_b = make_bundle(tmp_path / "src_b", num_pages=2)

    # Edit src_b's manifest to claim src_a's id (mimics a content-collision
    # situation). Note: this also changes the manifest's checksum file list,
    # but only the `id` field — bundle_upload's collision check only cares
    # about id and content_hash.
    manifest_a = json.loads((src_a / "manifest.json").read_text())
    manifest_b = json.loads((src_b / "manifest.json").read_text())
    manifest_b["id"] = manifest_a["id"]
    (src_b / "manifest.json").write_text(json.dumps(manifest_b))
    # Re-zip src_b with the doctored manifest. We also need src_b's bundle
    # directory to be renamed so zip_bundle's Shape A uses the new id.
    renamed = src_b.parent / manifest_a["id"]
    src_b.rename(renamed)
    zip_b = zip_bundle(renamed, tmp_path / "b.zip", shape="A")
    zip_a = zip_bundle(src_a, tmp_path / "a.zip", shape="A")

    final_root = tmp_path / "final"
    final_root.mkdir()

    extract_and_stage(zip_a, final_root, max_uncompressed_bytes=_2_GB)
    with pytest.raises(BundleUploadError, match="already exists with different content"):
        extract_and_stage(zip_b, final_root, max_uncompressed_bytes=_2_GB)

    # No residue.
    leftovers = [p.name for p in final_root.iterdir() if p.name.startswith(".upload-")]
    assert leftovers == []


def test_checksum_mismatch_is_rejected(tmp_path):
    src = make_bundle(tmp_path / "src")
    # Corrupt one of the page files before zipping.
    corrupt = src / "pages" / "0001.webp"
    corrupt.write_bytes(b"corrupted bytes")
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    final_root = tmp_path / "final"
    final_root.mkdir()
    with pytest.raises(BundleUploadError, match="checksum mismatch"):
        extract_and_stage(zip_path, final_root, max_uncompressed_bytes=_2_GB)

    # No residue, no published bundle.
    assert list(final_root.iterdir()) == []


def test_missing_file_referenced_in_manifest_is_rejected(tmp_path):
    src = make_bundle(tmp_path / "src")
    # Delete a file the manifest still references.
    (src / "pages" / "0001.webp").unlink()
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    final_root = tmp_path / "final"
    final_root.mkdir()
    with pytest.raises(BundleUploadError, match="file listed in manifest is missing"):
        extract_and_stage(zip_path, final_root, max_uncompressed_bytes=_2_GB)

    assert list(final_root.iterdir()) == []


# --- Route tests ---

def test_get_upload_form_requires_admin(app_client):
    client, _ = app_client
    resp = client.get("/admin/issues/upload", follow_redirects=False)
    # Anonymous → redirected to /login.
    assert resp.status_code == 303
    assert "/login" in resp.headers["location"]


def test_get_upload_form_renders_for_admin(admin_client):
    client, _ = admin_client
    resp = client.get("/admin/issues/upload")
    assert resp.status_code == 200
    assert 'name="bundle"' in resp.text
    assert 'type="file"' in resp.text
    assert 'name="csrf_token"' in resp.text


def _get_csrf(client) -> str:
    """Pull a CSRF token from the upload form."""
    resp = client.get("/admin/issues/upload")
    assert resp.status_code == 200
    import re
    m = re.search(r'name="csrf_token" value="([^"]+)"', resp.text)
    assert m, "csrf token not on upload form"
    return m.group(1)


def test_post_upload_imports_new_bundle(admin_client, tmp_path):
    client, bundles_dir = admin_client
    src = make_bundle(tmp_path / "src", title="ZX Spectrum", num_pages=1)
    bundle_id = src.name
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    token = _get_csrf(client)
    with zip_path.open("rb") as fp:
        resp = client.post(
            "/admin/issues/upload",
            data={"csrf_token": token},
            files={"bundle": ("upload.zip", fp, "application/zip")},
            follow_redirects=False,
        )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == f"/admin/issues/{bundle_id}/edit"

    # Bundle dir on disk.
    assert (bundles_dir / bundle_id / "manifest.json").exists()

    # DB row exists.
    settings = get_settings()
    factory = make_session_factory(make_engine(settings.database_url))
    with session_scope(factory) as s:
        mag = s.scalar(select(Magazine).where(Magazine.id == bundle_id))
        assert mag is not None
        assert mag.title == "ZX Spectrum"
