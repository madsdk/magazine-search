import re
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from magsearch.cli import app

runner = CliRunner()


def test_cli_help_lists_subcommands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("ingest", "import", "web", "db"):
        assert cmd in result.stdout


def test_cli_ingest_then_import_creates_searchable_magazine(tmp_path, monkeypatch):
    from tests.fixtures.pdfs import make_pdf

    db_path = tmp_path / "test.db"
    bundles = tmp_path / "bundles"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(bundles))

    src = make_pdf(tmp_path / "byte.pdf", num_pages=1)

    r = runner.invoke(app, ["db", "upgrade"])
    assert r.exit_code == 0, r.stdout

    r = runner.invoke(app, [
        "ingest", str(src),
        "--title", "Byte",
        "--date", "1985-12-01",
        "--bundles-dir", str(bundles),
        "--fake-ocr",
    ])
    assert r.exit_code == 0, r.stdout
    bundle = bundles / "byte-1985-12"
    assert (bundle / "manifest.json").exists()

    r = runner.invoke(app, ["import", str(bundle)])
    assert r.exit_code == 0, r.stdout
    assert "byte-1985-12" in r.stdout


def test_cli_ingest_cbr_without_rar_backend_exits_with_actionable_message(
    tmp_path, monkeypatch, no_rar_backend
):
    """No traceback — the user gets told which tool to install."""
    from tests.fixtures.cbrs import make_cbr

    src = make_cbr(tmp_path / "byte.cbr")
    r = runner.invoke(app, [
        "ingest", str(src),
        "--title", "Byte",
        "--date", "1985-12-01",
        "--bundles-dir", str(tmp_path / "bundles"),
        "--fake-ocr",
    ])
    assert r.exit_code == 2, r.output
    assert "apt-get install unar" in r.output
    assert "Traceback" not in r.output


def test_ocr_rescale_skips_a_bundle_whose_page_count_drifted(tmp_path, monkeypatch):
    """A repaired bundle has fewer pages than its archive; pairing them
    positionally would rescale each page against the wrong source image."""
    import json

    from tests.fixtures.bundles import make_bundle

    bundle = make_bundle(tmp_path, num_pages=3)
    # Put the OCR JSONs back in the old array format so there is work to skip.
    for ocr_path in (bundle / "ocr").glob("*.json"):
        doc = json.loads(ocr_path.read_text())
        ocr_path.write_text(json.dumps(doc["regions"]))
    # Simulate a repaired bundle: manifest says 2 pages, original.pdf has 3.
    data = json.loads((bundle / "manifest.json").read_text())
    data["page_count"] = 2
    data["pages"] = data["pages"][:2]
    (bundle / "manifest.json").write_text(json.dumps(data))

    r = runner.invoke(app, ["ocr-rescale", str(bundle.parent)])

    assert r.exit_code == 0, r.stdout
    assert "does not match the archive" in r.output
    # Untouched: still the old array format.
    assert isinstance(json.loads((bundle / "ocr" / "0001.json").read_text()), list)


def test_ocr_rescale_still_processes_a_consistent_bundle(tmp_path, monkeypatch):
    import json

    from tests.fixtures.bundles import make_bundle

    bundle = make_bundle(tmp_path, num_pages=2)
    for ocr_path in (bundle / "ocr").glob("*.json"):
        doc = json.loads(ocr_path.read_text())
        ocr_path.write_text(json.dumps(doc["regions"]))

    r = runner.invoke(app, ["ocr-rescale", str(bundle.parent)])

    assert r.exit_code == 0, r.stdout
    assert isinstance(json.loads((bundle / "ocr" / "0001.json").read_text()), dict)
