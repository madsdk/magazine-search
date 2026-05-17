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
