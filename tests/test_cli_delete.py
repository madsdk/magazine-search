from pathlib import Path

from typer.testing import CliRunner

from magsearch.cli import app

runner = CliRunner()


def _ingest_import(tmp_path, bundles, title, date_str, monkeypatch):
    from tests.fixtures.pdfs import make_pdf
    src = make_pdf(tmp_path / f"{title}-{date_str}.pdf", num_pages=1)
    r = runner.invoke(app, [
        "ingest", str(src), "--title", title, "--date", date_str,
        "--bundles-dir", str(bundles), "--fake-ocr",
    ])
    assert r.exit_code == 0, r.stdout
    # id slug is <title-lower>-<yyyy-mm>
    slug = f"{title.lower()}-{date_str[:7]}"
    r = runner.invoke(app, ["import", str(bundles / slug)])
    assert r.exit_code == 0, r.stdout
    return slug


def _env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    bundles = tmp_path / "bundles"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(bundles))
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    return bundles


def _counts(db_path):
    import sqlite3
    c = sqlite3.connect(db_path)
    try:
        mags = c.execute("SELECT COUNT(*) FROM magazines").fetchone()[0]
        pages = c.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        fts = c.execute("SELECT COUNT(*) FROM pages_fts").fetchone()[0]
        return mags, pages, fts
    finally:
        c.close()


def test_delete_by_id_removes_db_fts_and_files(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01", monkeypatch)
    assert (bundles / slug).exists()
    r = runner.invoke(app, ["delete", slug, "--yes"])
    assert r.exit_code == 0, r.stdout
    assert not (bundles / slug).exists()
    assert _counts(tmp_path / "test.db") == (0, 0, 0)


def test_delete_by_title_removes_all_issues_leaves_others(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    a = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01", monkeypatch)
    b = _ingest_import(tmp_path, bundles, "Byte", "1986-01-01", monkeypatch)
    other = _ingest_import(tmp_path, bundles, "Amiga", "1990-06-01", monkeypatch)
    r = runner.invoke(app, ["delete", "--title", "byte", "--yes"])
    assert r.exit_code == 0, r.stdout
    assert not (bundles / a).exists() and not (bundles / b).exists()
    assert (bundles / other).exists()
    mags, _, _ = _counts(tmp_path / "test.db")
    assert mags == 1


def test_delete_unknown_id_warns_but_deletes_valid(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01", monkeypatch)
    r = runner.invoke(app, ["delete", slug, "does-not-exist", "--yes"])
    assert r.exit_code == 0, r.stdout
    assert "does-not-exist" in r.output
    assert not (bundles / slug).exists()


def test_delete_no_selector_exits_2(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    r = runner.invoke(app, ["delete"])
    assert r.exit_code == 2


def test_delete_no_match_exits_1(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    r = runner.invoke(app, ["delete", "ghost-2000-01"])
    assert r.exit_code == 1


def test_delete_prompt_decline_leaves_data_intact(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01", monkeypatch)
    r = runner.invoke(app, ["delete", slug], input="n\n")
    assert r.exit_code != 0  # aborted
    assert (bundles / slug).exists()
    assert _counts(tmp_path / "test.db")[0] == 1
