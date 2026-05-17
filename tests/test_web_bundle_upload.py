"""Tests for src/magsearch/web/bundle_upload.py.

Pure-logic tests (this file) drive `extract_and_stage` directly with hand-rolled
zip files. Route-level tests come later via FastAPI's TestClient.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

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
