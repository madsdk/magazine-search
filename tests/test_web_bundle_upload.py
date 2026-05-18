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


def test_post_upload_renders_error_on_bad_zip(admin_client, tmp_path):
    client, _ = admin_client
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"this is not a zip file")

    token = _get_csrf(client)
    with bad.open("rb") as fp:
        resp = client.post(
            "/admin/issues/upload",
            data={"csrf_token": token},
            files={"bundle": ("bad.zip", fp, "application/zip")},
            follow_redirects=False,
        )

    assert resp.status_code == 400
    assert "not a valid zip" in resp.text
    # The form is still on the page so the admin can retry.
    assert 'name="bundle"' in resp.text


def test_post_upload_renders_error_on_size_limit(admin_client, tmp_path, monkeypatch):
    # Cap the limit to 10 bytes for this test so any real bundle blows it.
    monkeypatch.setenv("MAGSEARCH_MAX_UPLOAD_BYTES", "10")
    # Reset cached settings/deps so the override takes effect.
    from magsearch.web import deps as _deps
    _deps._session_factory_for.cache_clear()

    client, _ = admin_client
    src = make_bundle(tmp_path / "src")
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    token = _get_csrf(client)
    with zip_path.open("rb") as fp:
        resp = client.post(
            "/admin/issues/upload",
            data={"csrf_token": token},
            files={"bundle": ("upload.zip", fp, "application/zip")},
            follow_redirects=False,
        )
    # With the body-cap in place, a 10-byte limit fires as 413 (body too large)
    # before extract_and_stage even sees the data.  Either way the size limit
    # is enforced; accept both 400 and 413.
    assert resp.status_code in (400, 413)
    assert "exceed" in resp.text.lower()


def test_post_upload_requires_admin(user_client, tmp_path):
    """Signed-in non-admins are forbidden."""
    client, _ = user_client
    src = make_bundle(tmp_path / "src")
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    # We still need a CSRF token; user_client is signed in but not admin.
    # Grab one from /login (allow-listed, always renders a form).
    from tests.conftest import get_csrf
    token = get_csrf(client)

    with zip_path.open("rb") as fp:
        resp = client.post(
            "/admin/issues/upload",
            data={"csrf_token": token},
            files={"bundle": ("upload.zip", fp, "application/zip")},
            follow_redirects=False,
        )
    assert resp.status_code == 403


def test_post_upload_rejects_missing_csrf(admin_client, tmp_path):
    client, _ = admin_client
    src = make_bundle(tmp_path / "src")
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    with zip_path.open("rb") as fp:
        # No csrf_token in data.
        resp = client.post(
            "/admin/issues/upload",
            data={},
            files={"bundle": ("upload.zip", fp, "application/zip")},
            follow_redirects=False,
        )
    # FastAPI returns 422 if a required Form field is absent; the CSRF
    # dependency reads `csrf_token` via Form(...), so an absent token
    # triggers 422 (missing form field).
    assert resp.status_code in (403, 422)


def test_post_upload_idempotent_reupload(admin_client, tmp_path):
    client, bundles_dir = admin_client
    src = make_bundle(tmp_path / "src", title="Atari Age", num_pages=1)
    bundle_id = src.name
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")

    # First upload.
    token = _get_csrf(client)
    with zip_path.open("rb") as fp:
        first = client.post(
            "/admin/issues/upload",
            data={"csrf_token": token},
            files={"bundle": ("upload.zip", fp, "application/zip")},
            follow_redirects=False,
        )
    assert first.status_code == 303

    # Second upload of the identical zip → also redirects, no error.
    token = _get_csrf(client)
    with zip_path.open("rb") as fp:
        second = client.post(
            "/admin/issues/upload",
            data={"csrf_token": token},
            files={"bundle": ("upload.zip", fp, "application/zip")},
            follow_redirects=False,
        )
    assert second.status_code == 303
    assert second.headers["location"] == f"/admin/issues/{bundle_id}/edit"

    # Only one DB row.
    settings = get_settings()
    factory = make_session_factory(make_engine(settings.database_url))
    with session_scope(factory) as s:
        rows = s.scalars(select(Magazine).where(Magazine.id == bundle_id)).all()
        assert len(rows) == 1


def test_issues_index_links_to_upload(admin_client):
    client, _ = admin_client
    resp = client.get("/admin/issues")
    assert resp.status_code == 200
    assert "/admin/issues/upload" in resp.text


def test_post_upload_rejects_body_exceeding_limit(admin_client, tmp_path, monkeypatch):
    """The body byte count is enforced before the upload is fully spooled to disk.

    We don't need to actually upload more than the limit — we just need to assert
    that an oversized body produces a 413 with the right error.
    """
    monkeypatch.setenv("MAGSEARCH_MAX_UPLOAD_BYTES", "100")  # 100 bytes
    from magsearch.web import deps as _deps
    _deps._session_factory_for.cache_clear()

    client, _ = admin_client
    big = tmp_path / "big.zip"
    big.write_bytes(b"x" * 5_000)  # ~5 KB, well over the 100-byte cap

    token = _get_csrf(client)
    with big.open("rb") as fp:
        resp = client.post(
            "/admin/issues/upload",
            data={"csrf_token": token},
            files={"bundle": ("big.zip", fp, "application/zip")},
            follow_redirects=False,
        )
    assert resp.status_code == 413
    assert "exceeds" in resp.text.lower() or "too large" in resp.text.lower() or "exceed" in resp.text.lower()


def test_cross_filesystem_rename_is_rejected_with_friendly_error(tmp_path, monkeypatch):
    """If os.rename raises EXDEV (cross-filesystem rename), bundle_upload should
    surface it as a BundleUploadError rather than letting the bare OSError escape.
    """
    import os
    import errno
    from magsearch.web import bundle_upload as bu

    src = make_bundle(tmp_path / "src")
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")
    final_root = tmp_path / "final"
    final_root.mkdir()

    real_rename = os.rename

    def fake_rename(src, dst):
        # Simulate EXDEV. real_rename is fine for the path resolve etc.,
        # but at the actual atomic-publish call, raise EXDEV.
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(bu.os, "rename", fake_rename)
    with pytest.raises(BundleUploadError, match="different filesystems"):
        extract_and_stage(zip_path, final_root, max_uncompressed_bytes=_2_GB)
    # Staging dir cleaned up.
    leftovers = [p.name for p in final_root.iterdir() if p.name.startswith(".upload-")]
    assert leftovers == []


def _zip_with_doctored_id(src_bundle: Path, doctored_id: str, zip_path: Path) -> Path:
    """Re-zip a real bundle Shape B (contents at zip root) after overwriting
    its manifest id. We use Shape B so the on-disk directory does not need to
    be renamed to the (potentially malicious) id."""
    manifest = json.loads((src_bundle / "manifest.json").read_text())
    manifest["id"] = doctored_id
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for path in sorted(src_bundle.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(src_bundle).as_posix()
            if rel == "manifest.json":
                continue
            zf.write(path, arcname=rel)
    return zip_path


def test_manifest_id_parent_traversal_is_rejected(tmp_path):
    """manifest.id == '../escape' must not publish a bundle outside bundles_dir."""
    src = make_bundle(tmp_path / "src")
    zip_path = _zip_with_doctored_id(src, "../escape", tmp_path / "evil.zip")
    final_root = tmp_path / "final"
    final_root.mkdir()

    with pytest.raises(BundleUploadError, match="simple name|valid path component"):
        extract_and_stage(zip_path, final_root, max_uncompressed_bytes=_2_GB)

    # No published bundle and no escape: nothing outside final_root was written.
    assert list(final_root.iterdir()) == []
    assert not (final_root.parent / "escape").exists()


def test_manifest_id_absolute_path_is_rejected(tmp_path):
    """manifest.id == '/abs/path' must not publish a bundle at that absolute path.

    We point the absolute path at a sentinel under tmp_path so the test stays
    hermetic even if the validation regressed and the bundle did get written.
    """
    src = make_bundle(tmp_path / "src")
    sentinel = tmp_path / "sentinel"
    zip_path = _zip_with_doctored_id(src, str(sentinel), tmp_path / "evil.zip")
    final_root = tmp_path / "final"
    final_root.mkdir()

    with pytest.raises(BundleUploadError, match="simple name|valid path component"):
        extract_and_stage(zip_path, final_root, max_uncompressed_bytes=_2_GB)

    assert not sentinel.exists()
    assert list(final_root.iterdir()) == []


def test_oversized_manifest_is_rejected_before_decompression(tmp_path):
    """A manifest.json that decompresses to more than the per-entry cap must
    be rejected on the header check, before the bytes are inflated into memory.

    We craft a zip whose manifest.json is mostly NUL bytes — those compress to
    a tiny on-disk entry under ZIP_DEFLATED, but ZipInfo.file_size reflects
    the full uncompressed size. If the cap check were missing, zf.read on
    this entry would allocate the whole 80 MB.
    """
    z = tmp_path / "manifest-bomb.zip"
    big = b"\x00" * (80 * 1024 * 1024)  # 80 MB, > the 64 MB manifest cap
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", big)
    # Sanity check the fixture: heavily compressed on disk.
    assert z.stat().st_size < 1024 * 1024, "fixture didn't compress as expected"

    final_root = tmp_path / "final"
    final_root.mkdir()
    with pytest.raises(BundleUploadError, match="manifest.json is larger than"):
        extract_and_stage(z, final_root, max_uncompressed_bytes=_2_GB)
    assert list(final_root.iterdir()) == []


def test_manifest_id_nested_subpath_is_rejected(tmp_path):
    """manifest.id == 'nested/sub' resolves to a path under bundles_dir but
    not as a *direct* child. bulk_import walks `bundles_dir/<id>/` flat, so a
    nested id would break that contract — reject up front."""
    src = make_bundle(tmp_path / "src")
    zip_path = _zip_with_doctored_id(src, "nested/sub", tmp_path / "evil.zip")
    final_root = tmp_path / "final"
    final_root.mkdir()

    with pytest.raises(BundleUploadError, match="simple name"):
        extract_and_stage(zip_path, final_root, max_uncompressed_bytes=_2_GB)

    assert list(final_root.iterdir()) == []


def test_oversized_body_rejected_by_middleware_before_route(admin_client, tmp_path, monkeypatch):
    """The body cap must fire at the ASGI layer, before Starlette's multipart
    parser has a chance to spool gigabytes of attacker-supplied data to disk.

    We verify this two ways:
      1. The response is plain text (from the middleware), not the route's
         HTML error template — proving the route never ran.
      2. We patch `extract_and_stage` to record calls; it must not be called.
    """
    monkeypatch.setenv("MAGSEARCH_MAX_UPLOAD_BYTES", "100")
    from magsearch.web import deps as _deps
    _deps._session_factory_for.cache_clear()

    calls: list[object] = []
    from magsearch.web import routes_admin
    real_extract = routes_admin.extract_and_stage

    def spy_extract(*a, **kw):
        calls.append((a, kw))
        return real_extract(*a, **kw)

    monkeypatch.setattr(routes_admin, "extract_and_stage", spy_extract)

    client, _ = admin_client
    big = tmp_path / "big.zip"
    big.write_bytes(b"x" * 5_000)

    token = _get_csrf(client)
    with big.open("rb") as fp:
        resp = client.post(
            "/admin/issues/upload",
            data={"csrf_token": token},
            files={"bundle": ("big.zip", fp, "application/zip")},
            follow_redirects=False,
        )

    assert resp.status_code == 413
    # Middleware response is plain text; the route would have rendered HTML.
    assert resp.headers.get("content-type", "").startswith("text/plain")
    assert "exceeds maximum size" in resp.text
    # Route never ran → extract_and_stage was never invoked.
    assert calls == []


def test_corrupt_manifest_member_returns_friendly_error(tmp_path, monkeypatch):
    """zipfile.BadZipFile can fire during a member *read* (bad CRC, truncated
    deflate stream), not just at open time. Those failures must map to a
    BundleUploadError so the route returns 400 rather than crashing with 500.

    We simulate a corrupt manifest entry by patching ZipFile.open so reading
    the manifest raises BadZipFile mid-stream.
    """
    src = make_bundle(tmp_path / "src")
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")
    final_root = tmp_path / "final"
    final_root.mkdir()

    real_open = zipfile.ZipFile.open

    def broken_open(self, name_or_info, *args, **kwargs):
        f = real_open(self, name_or_info, *args, **kwargs)
        name = name_or_info.filename if isinstance(name_or_info, zipfile.ZipInfo) else name_or_info
        if name.endswith("manifest.json"):
            def bad_read(*_a, **_k):
                raise zipfile.BadZipFile("simulated bad CRC")
            f.read = bad_read
        return f

    monkeypatch.setattr(zipfile.ZipFile, "open", broken_open)

    with pytest.raises(BundleUploadError, match="manifest.json is corrupt"):
        extract_and_stage(zip_path, final_root, max_uncompressed_bytes=_2_GB)
    assert list(final_root.iterdir()) == []


def test_corrupt_member_during_extraction_returns_friendly_error(tmp_path, monkeypatch):
    """Same defense, but for a non-manifest member. The manifest reads cleanly
    so we proceed to staging; the corruption fires during _extract_under_prefix
    and must still surface as a BundleUploadError."""
    src = make_bundle(tmp_path / "src")
    zip_path = zip_bundle(src, tmp_path / "upload.zip", shape="A")
    final_root = tmp_path / "final"
    final_root.mkdir()

    real_open = zipfile.ZipFile.open

    def broken_open(self, name_or_info, *args, **kwargs):
        f = real_open(self, name_or_info, *args, **kwargs)
        name = name_or_info.filename if isinstance(name_or_info, zipfile.ZipInfo) else name_or_info
        # Manifest reads cleanly; the first non-manifest payload member blows up.
        if name.endswith("cover.webp"):
            def bad_read(*_a, **_k):
                raise zipfile.BadZipFile("simulated truncated deflate stream")
            f.read = bad_read
        return f

    monkeypatch.setattr(zipfile.ZipFile, "open", broken_open)

    with pytest.raises(BundleUploadError, match="zip member is corrupt"):
        extract_and_stage(zip_path, final_root, max_uncompressed_bytes=_2_GB)

    # No residue: staging dir must be cleaned up even though we got past
    # validation before failing.
    leftovers = [p.name for p in final_root.iterdir() if p.name.startswith(".upload-")]
    assert leftovers == []
    # And no published bundle.
    published = [p for p in final_root.iterdir() if not p.name.startswith(".upload-")]
    assert published == []
