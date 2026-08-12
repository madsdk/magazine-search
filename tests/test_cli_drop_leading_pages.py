import json
from pathlib import Path

from typer.testing import CliRunner

from magsearch.cli import app

runner = CliRunner()

# Assertions on message text use `result.output` (stdout + stderr combined)
# rather than `result.stdout`: the command writes refusals with `err=True`, and
# whether those land in `result.stdout` depends on the Click version.


def _env(tmp_path, monkeypatch) -> Path:
    db_path = tmp_path / "test.db"
    bundles = tmp_path / "bundles"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(bundles))
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    return bundles


def _ingest_import(tmp_path, bundles, title, date_str, num_pages=3) -> str:
    from tests.fixtures.pdfs import make_pdf
    src = make_pdf(tmp_path / f"{title}-{date_str}.pdf", num_pages=num_pages)
    r = runner.invoke(app, [
        "ingest", str(src), "--title", title, "--date", date_str,
        "--bundles-dir", str(bundles), "--fake-ocr",
    ])
    assert r.exit_code == 0, r.stdout
    slug = f"{title.lower()}-{date_str[:7]}"
    r = runner.invoke(app, ["import", str(bundles / slug)])
    assert r.exit_code == 0, r.stdout
    return slug


def _page_count(bundles, slug) -> int:
    return json.loads((bundles / slug / "manifest.json").read_text())["page_count"]


def test_help_lists_the_command():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "drop-leading-pages" in result.stdout


def test_dry_run_reports_and_changes_nothing(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")

    r = runner.invoke(app, ["drop-leading-pages", slug, "--dry-run"])

    assert r.exit_code == 0, r.stdout
    assert "drop page 1" in r.output
    assert "3 pages → 2" in r.output
    assert _page_count(bundles, slug) == 3


def test_dry_run_shows_the_page_text(tmp_path, monkeypatch):
    """Seeing the dropped page's text is how the operator tells a credits
    sheet from a cover, so the preview must be exact, not merely present.
    `--fake-ocr` builds FakeOCREngine with no scripted responses, so every
    page's text is its default, "fake page" (ocr.py:72)."""
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")

    r = runner.invoke(app, ["drop-leading-pages", slug, "--dry-run"])

    assert 'drop page 1  pages/0001.webp  "fake page"' in r.output


def test_drop_repairs_bundle_and_database(tmp_path, monkeypatch):
    import sqlite3

    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")

    r = runner.invoke(app, ["drop-leading-pages", slug, "--yes"])

    assert r.exit_code == 0, r.stdout
    assert _page_count(bundles, slug) == 2
    assert not (bundles / slug / "pages" / "0003.webp").exists()
    c = sqlite3.connect(tmp_path / "test.db")
    try:
        assert c.execute("SELECT page_count FROM magazines").fetchone()[0] == 2
        assert [r[0] for r in c.execute(
            "SELECT page_number FROM pages ORDER BY page_number")] == [1, 2]
        assert c.execute("SELECT COUNT(*) FROM pages_fts").fetchone()[0] == 2
    finally:
        c.close()


def test_repaired_bundle_passes_check(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")

    assert runner.invoke(app, ["drop-leading-pages", slug, "--yes"]).exit_code == 0

    r = runner.invoke(app, ["check", slug, "--checksums"])
    assert r.exit_code == 0, r.stdout


def test_count_flag_drops_two(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01", num_pages=4)

    r = runner.invoke(app, ["drop-leading-pages", slug, "--count", "2", "--yes"])

    assert r.exit_code == 0, r.stdout
    assert _page_count(bundles, slug) == 2


def test_prompt_aborts_without_changes(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")

    r = runner.invoke(app, ["drop-leading-pages", slug], input="n\n")

    assert r.exit_code != 0
    assert _page_count(bundles, slug) == 3


def test_unknown_bundle_is_reported_and_others_still_process(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    good = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")

    r = runner.invoke(app, ["drop-leading-pages", "no-such-bundle", good, "--yes"])

    assert r.exit_code == 1, r.stdout
    assert "no-such-bundle" in r.output
    assert _page_count(bundles, good) == 2


def test_requires_at_least_one_id(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)

    r = runner.invoke(app, ["drop-leading-pages"])

    assert r.exit_code == 2
    assert "specify at least one magazine ID" in r.output


def test_bundle_without_db_row_is_repaired_with_a_warning(tmp_path, monkeypatch):
    from tests.fixtures.pdfs import make_pdf

    bundles = _env(tmp_path, monkeypatch)
    src = make_pdf(tmp_path / "orphan.pdf", num_pages=3)
    r = runner.invoke(app, [
        "ingest", str(src), "--title", "Orphan", "--date", "1985-12-01",
        "--bundles-dir", str(bundles), "--fake-ocr",
    ])
    assert r.exit_code == 0, r.stdout

    r = runner.invoke(app, ["drop-leading-pages", "orphan-1985-12", "--yes"])

    assert r.exit_code == 0, r.stdout
    assert "not in database" in r.output
    assert _page_count(bundles, "orphan-1985-12") == 2


def test_refusal_leaves_the_bundle_intact(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    (bundles / slug / "pages" / "0002.webp").write_bytes(b"corrupted")

    r = runner.invoke(app, ["drop-leading-pages", slug, "--yes"])

    assert r.exit_code == 1
    assert "checksum mismatch" in r.output
    assert _page_count(bundles, slug) == 3
