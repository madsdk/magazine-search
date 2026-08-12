import json
import sqlite3
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


def _bundle_files(bundle_dir: Path) -> dict[str, bytes]:
    """Byte snapshot of every file in a bundle directory, for before/after
    comparisons that must prove nothing on disk changed."""
    return {
        str(p.relative_to(bundle_dir)): p.read_bytes()
        for p in sorted(bundle_dir.rglob("*"))
        if p.is_file()
    }


def _db_state(tmp_path, magazine_id) -> tuple:
    """(page_count, [page_number, ...]) for one magazine, for before/after
    comparisons that must prove the database wasn't touched."""
    c = sqlite3.connect(tmp_path / "test.db")
    try:
        row = c.execute(
            "SELECT page_count FROM magazines WHERE id = ?", (magazine_id,)
        ).fetchone()
        page_count = row[0] if row else None
        numbers = [
            n for (n,) in c.execute(
                "SELECT page_number FROM pages WHERE magazine_id = ? ORDER BY page_number",
                (magazine_id,),
            )
        ]
        return (page_count, numbers)
    finally:
        c.close()


def _no_stray_dirs(bundles: Path, slug: str) -> bool:
    return not (bundles / f"{slug}.new").exists() and not (bundles / f"{slug}.old").exists()


def test_help_lists_the_command():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "drop-leading-pages" in result.stdout


def test_dry_run_reports_and_changes_nothing(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    files_before = _bundle_files(bundles / slug)
    db_before = _db_state(tmp_path, slug)

    r = runner.invoke(app, ["drop-leading-pages", slug, "--dry-run"])

    assert r.exit_code == 0, r.stdout
    assert "drop page 1" in r.output
    assert "3 pages → 2" in r.output
    assert _page_count(bundles, slug) == 3
    assert _bundle_files(bundles / slug) == files_before
    assert _db_state(tmp_path, slug) == db_before
    assert _no_stray_dirs(bundles, slug)


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
    files_before = _bundle_files(bundles / slug)
    db_before = _db_state(tmp_path, slug)

    r = runner.invoke(app, ["drop-leading-pages", slug], input="n\n")

    assert r.exit_code != 0
    assert _page_count(bundles, slug) == 3
    assert _bundle_files(bundles / slug) == files_before
    assert _db_state(tmp_path, slug) == db_before
    assert _no_stray_dirs(bundles, slug)


def test_confirm_prompt_accepts_and_repairs(tmp_path, monkeypatch):
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")

    r = runner.invoke(app, ["drop-leading-pages", slug], input="y\n")

    assert r.exit_code == 0, r.stdout
    assert _page_count(bundles, slug) == 2


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


def test_resync_failure_leaves_bundle_and_database_untouched(tmp_path, monkeypatch):
    """Proves the fixed transaction order: the on-disk swap must not happen
    before the database write. Fails against the pre-fix code, where
    apply_drop (the swap) ran before resync_magazine — a resync failure there
    left a repaired bundle with a stale database."""
    bundles = _env(tmp_path, monkeypatch)
    slug = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    files_before = _bundle_files(bundles / slug)
    db_before = _db_state(tmp_path, slug)

    def boom(session, manifest, count):
        raise RuntimeError("simulated resync failure")

    monkeypatch.setattr("magsearch.cli.resync_magazine", boom)

    r = runner.invoke(app, ["drop-leading-pages", slug, "--yes"])

    assert r.exit_code == 1, r.stdout
    assert "simulated resync failure" in r.output
    assert _bundle_files(bundles / slug) == files_before
    assert _db_state(tmp_path, slug) == db_before
    assert _no_stray_dirs(bundles, slug)


def test_mixed_outcome_first_committed_second_fails(tmp_path, monkeypatch):
    """A KeyError from resync_magazine (real drift between DB rows and the
    manifest) must be caught per-bundle, not just BundleEditError/OSError/
    DatabaseError: with the narrow tuple, an unhandled KeyError on the first
    id aborts the whole command, so the second id is never processed and no
    summary prints. Also proves session isolation: a later bundle's failure
    must not undo an earlier bundle's already-committed repair."""
    import magsearch.cli as cli_mod

    bundles = _env(tmp_path, monkeypatch)
    good = _ingest_import(tmp_path, bundles, "Byte", "1985-12-01")
    bad = _ingest_import(tmp_path, bundles, "Compute", "1986-01-01")
    real_resync = cli_mod.resync_magazine

    def flaky(session, manifest, count):
        if manifest.id == bad:
            raise KeyError("simulated drift between db rows and manifest")
        return real_resync(session, manifest, count)

    monkeypatch.setattr("magsearch.cli.resync_magazine", flaky)

    r = runner.invoke(app, ["drop-leading-pages", good, bad, "--yes"])

    assert r.exit_code == 1, r.stdout
    assert _page_count(bundles, good) == 2
    assert _db_state(tmp_path, good) == (2, [1, 2])
    assert _page_count(bundles, bad) == 3
    assert "1 bundle(s) repaired, 1 skipped" in r.output
