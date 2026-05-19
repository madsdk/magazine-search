"""Tests for the desktop-only /import route (multi-file bundle upload).

The /import router is only registered when MAGSEARCH_AUTH_ENABLED=false.
"""

import json
import zipfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from magsearch.web.app import create_app


@pytest.fixture
def desktop_client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(bundles))
    monkeypatch.setenv("MAGSEARCH_AUTH_ENABLED", "false")
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")

    from magsearch.web import deps
    deps._session_factory_for.cache_clear()

    return TestClient(create_app()), bundles


@pytest.fixture
def auth_on_client(tmp_path, monkeypatch):
    """Same setup but with auth_enabled=True (the default), to verify /import
    is *not* registered in web mode."""
    db_path = tmp_path / "test.db"
    bundles = tmp_path / "bundles"
    bundles.mkdir()
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(bundles))
    monkeypatch.setenv("MAGSEARCH_SESSION_SECRET", "test-secret")
    monkeypatch.delenv("MAGSEARCH_AUTH_ENABLED", raising=False)
    cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")

    from magsearch.web import deps
    deps._session_factory_for.cache_clear()

    return TestClient(create_app())


def _make_bundle_zip(tmp_path: Path, bundle_id: str = "test-001") -> Path:
    """Build a minimal valid bundle on disk, then zip it into a .magbundle."""
    from magsearch.ingest.ids import content_hash

    bundle_dir = tmp_path / "src" / bundle_id
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "pages").mkdir()
    (bundle_dir / "ocr").mkdir()
    page = bundle_dir / "pages" / "0001.webp"
    page.write_bytes(b"fake-webp-bytes")
    ocr = bundle_dir / "ocr" / "0001.json"
    ocr.write_text("[]")
    page_hash = content_hash(page)
    ocr_hash = content_hash(ocr)

    manifest = {
        "schema_version": 1,
        "id": bundle_id,
        "title": "Test Title",
        "issue": "1",
        "publication_date": None,
        "publisher": None,
        "original_filename": "test.pdf",
        "original_format": "pdf",
        "page_count": 1,
        "content_hash": "deadbeef" * 8,
        "cover_path": "pages/0001.webp",
        "ocr_engine": "fake",
        "ocr_engine_version": "0.0",
        "pages": [
            {
                "page_number": 1,
                "image_path": "pages/0001.webp",
                "thumb_path": "pages/0001.webp",
                "ocr_path": "ocr/0001.json",
                "text": "hello world",
            }
        ],
        "checksums": [
            {"path": "pages/0001.webp", "sha256": page_hash},
            {"path": "ocr/0001.json", "sha256": ocr_hash},
        ],
    }
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))

    zip_path = tmp_path / f"{bundle_id}.magbundle"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for p in bundle_dir.rglob("*"):
            if p.is_file():
                zf.write(p, arcname=p.relative_to(bundle_dir.parent / bundle_id))
    return zip_path


def test_import_form_renders_in_desktop_mode(desktop_client):
    client, _ = desktop_client
    r = client.get("/import")
    assert r.status_code == 200
    assert b'name="bundles"' in r.content
    assert b"multiple" in r.content


def test_import_route_absent_when_auth_enabled(auth_on_client):
    client = auth_on_client
    r = client.get("/import")
    assert r.status_code == 404


def test_import_one_bundle_succeeds(desktop_client, tmp_path):
    client, bundles_dir = desktop_client
    zip_path = _make_bundle_zip(tmp_path)
    with zip_path.open("rb") as f:
        r = client.post(
            "/import",
            files=[("bundles", (zip_path.name, f, "application/zip"))],
        )
    assert r.status_code == 200
    assert b"imported" in r.content
    assert b"test-001" in r.content
    assert (bundles_dir / "test-001" / "manifest.json").exists()


def test_import_continues_past_a_bad_bundle(desktop_client, tmp_path):
    client, bundles_dir = desktop_client
    good_zip = _make_bundle_zip(tmp_path, bundle_id="good-001")
    bad_zip = tmp_path / "bad.magbundle"
    bad_zip.write_bytes(b"not a zip file at all")

    with good_zip.open("rb") as gf, bad_zip.open("rb") as bf:
        r = client.post(
            "/import",
            files=[
                ("bundles", (bad_zip.name, bf, "application/zip")),
                ("bundles", (good_zip.name, gf, "application/zip")),
            ],
        )
    assert r.status_code == 200
    body = r.content.decode()
    assert "bad.magbundle" in body
    assert "good-001.magbundle" in body
    assert (bundles_dir / "good-001" / "manifest.json").exists()
