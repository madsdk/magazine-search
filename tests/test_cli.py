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
