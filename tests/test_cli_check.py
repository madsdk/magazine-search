import json
import shutil

from typer.testing import CliRunner

from magsearch.cli import app

runner = CliRunner()


def _env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    bundles = tmp_path / "bundles"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(bundles))
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    return bundles


def _ingest_import(tmp_path, bundles, title, date_str):
    from tests.fixtures.pdfs import make_pdf
    src = make_pdf(tmp_path / f"{title}-{date_str}.pdf", num_pages=2)
    r = runner.invoke(app, [
        "ingest", str(src), "--title", title, "--date", date_str,
        "--bundles-dir", str(bundles), "--fake-ocr",
    ])
    assert r.exit_code == 0, r.stdout
    slug = f"{title.lower()}-{date_str[:7]}"
    r = runner.invoke(app, ["import", str(bundles / slug)])
    assert r.exit_code == 0, r.stdout
    return slug


def test_clean_bundle_exits_zero(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    r = runner.invoke(app, ["check", slug])
    assert r.exit_code == 0, r.stdout
    assert "OK" in r.stdout


def test_missing_file_exits_one(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    (bundles / slug / "pages" / "0001.webp").unlink()
    r = runner.invoke(app, ["check", slug])
    assert r.exit_code == 1, r.stdout
    assert "0001.webp" in r.stdout


def test_checksums_flag_catches_corruption(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    # Corrupt a non-image file so only the checksum check fires.
    (bundles / slug / "original.pdf").write_bytes(b"tampered")
    assert runner.invoke(app, ["check", slug]).exit_code == 0
    r = runner.invoke(app, ["check", slug, "--checksums"])
    assert r.exit_code == 1, r.stdout
    assert "checksum" in r.stdout


def test_strict_promotes_warning_to_failure(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    # Rewrite one OCR file to legacy (array) format => warning, not error.
    (bundles / slug / "ocr" / "0001.json").write_text(
        json.dumps([{"text": "x", "bbox": [1, 2, 3, 4], "confidence": 1.0}])
    )
    assert runner.invoke(app, ["check", slug]).exit_code == 0
    r = runner.invoke(app, ["check", slug, "--strict"])
    assert r.exit_code == 1, r.stdout
    assert "legacy" in r.stdout


def test_whole_corpus_flags_orphan_dir(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    (bundles / "orphan-2000-01").mkdir()  # dir on disk, no DB row, no manifest
    r = runner.invoke(app, ["check"])  # no selector => audit everything
    assert r.exit_code == 1, r.stdout
    assert "orphan-2000-01" in r.stdout


def test_whole_corpus_flags_orphan_db_row(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    shutil.rmtree(bundles / slug)  # DB row remains, directory gone
    r = runner.invoke(app, ["check"])
    assert r.exit_code == 1, r.stdout
    assert slug in r.stdout


def test_unknown_id_exits_one(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    r = runner.invoke(app, ["check", "does-not-exist"])
    assert r.exit_code == 1, r.stdout
    assert "does-not-exist" in r.stdout


def test_corrupt_database_exits_two(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)  # creates and upgrades a real DB
    # Overwrite the SQLite file with non-DB bytes so any query raises DatabaseError.
    (tmp_path / "test.db").write_bytes(b"this is definitely not a sqlite database file")
    r = runner.invoke(app, ["check"])
    assert r.exit_code == 2, r.output
    assert "database error" in r.output.lower()
    # The reorder means the FTS check never runs on a dead DB, so no misleading line:
    assert "integrity check failed" not in r.output
