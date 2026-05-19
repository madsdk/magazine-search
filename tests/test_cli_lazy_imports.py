"""Regression test: importing magsearch.cli must not pull in the heavy
ingest-only deps (fitz / rarfile). They're only listed in the [ingest]
optional extra, so a base install + `magsearch web` must work without them.
"""

import importlib
import sys


def test_cli_import_does_not_load_fitz_or_rarfile():
    for name in ("fitz", "rarfile"):
        sys.modules.pop(name, None)
    for name in list(sys.modules):
        if name.startswith("magsearch.") or name == "magsearch":
            sys.modules.pop(name, None)

    importlib.import_module("magsearch.cli")

    assert "fitz" not in sys.modules, (
        "magsearch.cli pulled in fitz at module load — break the import chain "
        "by deferring magsearch.ingest.* imports into the ingest commands"
    )
    assert "rarfile" not in sys.modules, (
        "magsearch.cli pulled in rarfile at module load — break the import chain "
        "by deferring magsearch.ingest.* imports into the ingest commands"
    )


def test_web_app_factory_does_not_load_fitz_or_rarfile(monkeypatch, tmp_path):
    """Same guarantee for create_app(): the FastAPI app booting must not
    require ingest extras."""
    db = tmp_path / "t.db"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(tmp_path / "bundles"))
    monkeypatch.setenv("MAGSEARCH_SESSION_SECRET", "test")

    for name in ("fitz", "rarfile"):
        sys.modules.pop(name, None)
    for name in list(sys.modules):
        if name.startswith("magsearch.") or name == "magsearch":
            sys.modules.pop(name, None)

    web_app = importlib.import_module("magsearch.web.app")
    web_app.create_app()

    assert "fitz" not in sys.modules
    assert "rarfile" not in sys.modules
