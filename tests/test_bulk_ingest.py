import json
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from magsearch.cli import app
from magsearch.ingest.bulk import (
    DONE,
    FAILED,
    SidecarError,
    StateLog,
    bulk_ingest,
    load_sidecar,
    scan_inputs,
)
from magsearch.ingest.ocr import FakeOCREngine, FatalOCRError, OCRRegion
from tests.fixtures.pdfs import make_pdf

runner = CliRunner()


def _make_collection(root: Path, names_and_pages: list[tuple[str, int]]) -> list[Path]:
    root.mkdir(parents=True, exist_ok=True)
    return [
        make_pdf(root / name, num_pages=pages, page_text=[f"text-{name}-{i+1}" for i in range(pages)])
        for name, pages in names_and_pages
    ]


def _scripted_engine(num_responses: int = 32) -> FakeOCREngine:
    """A FakeOCREngine with enough responses for repeated calls across files."""
    return FakeOCREngine(responses=[
        [OCRRegion(text=f"page text {i}", bbox=(0, 0, 100, 20), confidence=1.0)]
        for i in range(num_responses)
    ])


# ---- sidecar parsing ------------------------------------------------------

def test_load_sidecar_csv_basic(tmp_path):
    csv_path = tmp_path / "metadata.csv"
    csv_path.write_text(
        "filename,title,issue,date,publisher\n"
        "byte-1985-12.pdf,Byte,Vol 10 No 12,1985-12-01,McGraw-Hill\n"
        "byte-1985-12-xmas.pdf,Byte,Christmas Double,1985-12-15,McGraw-Hill\n"
    )
    out = load_sidecar(csv_path)
    assert set(out) == {"byte-1985-12.pdf", "byte-1985-12-xmas.pdf"}
    assert out["byte-1985-12.pdf"].title == "Byte"
    assert out["byte-1985-12.pdf"].date == date(1985, 12, 1)
    assert out["byte-1985-12-xmas.pdf"].issue == "Christmas Double"


def test_load_sidecar_csv_tolerates_comments_and_blank_lines(tmp_path):
    csv_path = tmp_path / "metadata.csv"
    csv_path.write_text(
        "# pre-header comment\n"
        "filename,title,date\n"
        "\n"
        "# inline comment between rows\n"
        "byte.pdf,Byte,1985-12-01\n"
    )
    out = load_sidecar(csv_path)
    assert list(out) == ["byte.pdf"]


def test_load_sidecar_csv_missing_filename_column(tmp_path):
    csv_path = tmp_path / "metadata.csv"
    csv_path.write_text("title,date\nByte,1985-12-01\n")
    with pytest.raises(SidecarError, match="filename"):
        load_sidecar(csv_path)


def test_load_sidecar_csv_bad_date(tmp_path):
    csv_path = tmp_path / "metadata.csv"
    csv_path.write_text("filename,date\nbyte.pdf,not-a-date\n")
    with pytest.raises(SidecarError, match="bad date"):
        load_sidecar(csv_path)


def test_load_sidecar_csv_duplicate_filename(tmp_path):
    csv_path = tmp_path / "metadata.csv"
    csv_path.write_text(
        "filename,title\nbyte.pdf,A\nbyte.pdf,B\n"
    )
    with pytest.raises(SidecarError, match="duplicate"):
        load_sidecar(csv_path)


def test_load_sidecar_json(tmp_path):
    json_path = tmp_path / "metadata.json"
    json_path.write_text(json.dumps([
        {"filename": "byte.pdf", "title": "Byte", "date": "1985-12-01"},
    ]))
    out = load_sidecar(json_path)
    assert out["byte.pdf"].date == date(1985, 12, 1)


# ---- scanning -------------------------------------------------------------

def test_scan_inputs_filters_to_supported_formats(tmp_path):
    _make_collection(tmp_path, [("a.pdf", 1), ("b.pdf", 1)])
    (tmp_path / "metadata.csv").write_text("filename\n")
    (tmp_path / "readme.txt").write_text("ignore me")
    files = scan_inputs(tmp_path, recursive=False)
    assert [p.name for p in files] == ["a.pdf", "b.pdf"]


# ---- state log ------------------------------------------------------------

def test_state_log_round_trip(tmp_path):
    path = tmp_path / "state.jsonl"
    log = StateLog(path)
    e = log.get("a.pdf")
    e.status = DONE
    e.bundle_id = "a-1985-12"
    log.save()
    reloaded = StateLog(path)
    assert reloaded.get("a.pdf").status == DONE
    assert reloaded.get("a.pdf").bundle_id == "a-1985-12"


# ---- happy path -----------------------------------------------------------

def test_bulk_ingest_happy_path(tmp_path):
    src = tmp_path / "in"
    _make_collection(src, [("byte-1985-12.pdf", 1), ("byte-1986-01.pdf", 1)])
    (src / "metadata.csv").write_text(
        "filename,title,date,publisher\n"
        "byte-1985-12.pdf,Byte,1985-12-01,McGraw-Hill\n"
        "byte-1986-01.pdf,Byte,1986-01-01,McGraw-Hill\n"
    )
    bundles = tmp_path / "out"
    result = bulk_ingest(
        input_dir=src, sidecar_path=None, state_path=None,
        bundles_dir=bundles, ocr_engine=_scripted_engine(),
    )
    assert result.succeeded == 2
    assert result.failed == 0
    assert (bundles / "byte-1985-12" / "manifest.json").exists()
    assert (bundles / "byte-1986-01" / "manifest.json").exists()
    # State log was written.
    assert (src / ".bulk-state.jsonl").exists()


def test_bulk_ingest_resumes_after_partial_run(tmp_path):
    """Second invocation must skip files marked done in the state log."""
    src = tmp_path / "in"
    _make_collection(src, [("a.pdf", 1), ("b.pdf", 1)])
    (src / "metadata.csv").write_text(
        "filename,title,date\n"
        "a.pdf,A,1985-01-01\n"
        "b.pdf,B,1985-02-01\n"
    )
    bundles = tmp_path / "out"

    # First run: only process a.pdf by pre-seeding b.pdf as done in state.
    StateLog(src / ".bulk-state.jsonl")  # ensures path exists
    log = StateLog(src / ".bulk-state.jsonl")
    e = log.get("b.pdf")
    e.status = DONE
    e.bundle_id = "b-1985-02"
    log.save()

    result = bulk_ingest(
        input_dir=src, sidecar_path=None, state_path=None,
        bundles_dir=bundles, ocr_engine=_scripted_engine(),
    )
    assert result.succeeded == 1   # only a.pdf was actually ingested
    assert result.skipped == 1     # b.pdf was skipped
    assert (bundles / "a-1985-01" / "manifest.json").exists()
    # b.pdf was NOT processed — no bundle on disk for it.
    assert not (bundles / "b-1985-02" / "manifest.json").exists()


def test_bulk_ingest_retry_failed(tmp_path):
    src = tmp_path / "in"
    _make_collection(src, [("a.pdf", 1)])
    (src / "metadata.csv").write_text("filename,title,date\na.pdf,A,1985-01-01\n")
    bundles = tmp_path / "out"

    # Pre-seed state: a.pdf was failed last time.
    log = StateLog(src / ".bulk-state.jsonl")
    e = log.get("a.pdf")
    e.status = FAILED
    e.error = "synthetic"
    log.save()

    # Without --retry-failed it's skipped.
    r1 = bulk_ingest(
        input_dir=src, sidecar_path=None, state_path=None,
        bundles_dir=bundles, ocr_engine=_scripted_engine(),
    )
    assert r1.succeeded == 0 and r1.skipped == 1

    # With --retry-failed it's processed.
    r2 = bulk_ingest(
        input_dir=src, sidecar_path=None, state_path=None,
        bundles_dir=bundles, ocr_engine=_scripted_engine(),
        retry_failed=True,
    )
    assert r2.succeeded == 1


def test_bulk_ingest_strict_sidecar(tmp_path):
    src = tmp_path / "in"
    _make_collection(src, [("a.pdf", 1), ("b.pdf", 1)])
    (src / "metadata.csv").write_text("filename,title\na.pdf,A\n")  # b missing
    bundles = tmp_path / "out"

    # Lax mode: warns but proceeds.
    warnings: list[str] = []
    result = bulk_ingest(
        input_dir=src, sidecar_path=None, state_path=None,
        bundles_dir=bundles, ocr_engine=_scripted_engine(),
        on_warning=warnings.append,
    )
    assert result.succeeded == 2
    assert any("b.pdf" in w for w in warnings)

    # Strict mode: errors out.
    src2 = tmp_path / "in2"
    _make_collection(src2, [("a.pdf", 1), ("b.pdf", 1)])
    (src2 / "metadata.csv").write_text("filename,title\na.pdf,A\n")
    with pytest.raises(SidecarError, match="no sidecar row"):
        bulk_ingest(
            input_dir=src2, sidecar_path=None, state_path=None,
            bundles_dir=tmp_path / "out2", ocr_engine=_scripted_engine(),
            strict_sidecar=True,
        )


def test_bulk_ingest_dry_run_touches_nothing(tmp_path):
    src = tmp_path / "in"
    _make_collection(src, [("a.pdf", 1)])
    (src / "metadata.csv").write_text("filename,title,date\na.pdf,A,1985-01-01\n")
    bundles = tmp_path / "out"
    bundles_existed_before = bundles.exists()

    result = bulk_ingest(
        input_dir=src, sidecar_path=None, state_path=None,
        bundles_dir=bundles, ocr_engine=_scripted_engine(),
        dry_run=True,
    )
    assert result.succeeded == 0 and result.failed == 0
    # No bundles or state file written.
    assert bundles.exists() == bundles_existed_before
    assert not (src / ".bulk-state.jsonl").exists()


def test_bulk_ingest_sidecar_references_missing_file(tmp_path):
    src = tmp_path / "in"
    _make_collection(src, [("a.pdf", 1)])
    (src / "metadata.csv").write_text(
        "filename,title\na.pdf,A\nghost.pdf,Ghost\n"
    )
    with pytest.raises(SidecarError, match="ghost.pdf"):
        bulk_ingest(
            input_dir=src, sidecar_path=None, state_path=None,
            bundles_dir=tmp_path / "out", ocr_engine=_scripted_engine(),
        )


def test_bulk_ingest_continues_past_failure(tmp_path):
    """When one file fails, the batch keeps going by default."""
    src = tmp_path / "in"
    src.mkdir(parents=True, exist_ok=True)
    make_pdf(src / "good.pdf", num_pages=1)
    # A "file" with a recognized extension but garbage contents — pipeline
    # will fail format detection on it.
    bad = src / "bad.pdf"
    bad.write_bytes(b"not a real pdf")
    (src / "metadata.csv").write_text(
        "filename,title,date\n"
        "good.pdf,Good,1985-01-01\n"
        "bad.pdf,Bad,1985-02-01\n"
    )
    bundles = tmp_path / "out"
    result = bulk_ingest(
        input_dir=src, sidecar_path=None, state_path=None,
        bundles_dir=bundles, ocr_engine=_scripted_engine(),
    )
    assert result.succeeded == 1
    assert result.failed == 1
    # The good file still landed in bundles.
    assert (bundles / "good-1985-01" / "manifest.json").exists()


def test_bulk_ingest_aborts_whole_run_on_fatal_ocr_error(tmp_path):
    """A fatal OCR error must stop the entire batch (CUDA is dead process-wide),
    not merely fail one file and press on — otherwise every later file is
    silently ingested with empty text."""
    src = tmp_path / "in"
    _make_collection(src, [("a.pdf", 1), ("b.pdf", 1), ("c.pdf", 1)])
    (src / "metadata.csv").write_text(
        "filename,title,date\n"
        "a.pdf,A,1985-01-01\n"
        "b.pdf,B,1985-02-01\n"
        "c.pdf,C,1985-03-01\n"
    )
    bundles = tmp_path / "out"
    # a.pdf OCRs fine (1 page); b.pdf's only page hits a fatal GPU fault.
    engine = FakeOCREngine(responses=[
        [OCRRegion(text="ok", bbox=(0, 0, 100, 20), confidence=1.0)],
        FatalOCRError("unrecoverable GPU error during OCR: CUDA error(700)"),
    ])

    warnings: list[str] = []
    result = bulk_ingest(
        input_dir=src, sidecar_path=None, state_path=None,
        bundles_dir=bundles, ocr_engine=engine,
        on_warning=warnings.append,
    )

    assert result.aborted is True
    assert result.succeeded == 1          # a.pdf
    assert result.failed == 1             # b.pdf
    # c.pdf was never attempted — the run stopped at the fatal error.
    assert (bundles / "a-1985-01" / "manifest.json").exists()
    assert not (bundles / "b-1985-02" / "manifest.json").exists()
    assert not (bundles / "c-1985-03" / "manifest.json").exists()
    assert any("abort" in w.lower() for w in warnings)

    # State reflects reality: b failed, c is still pending (untouched).
    state = StateLog(src / ".bulk-state.jsonl")
    assert state.get("a.pdf").status == DONE
    assert state.get("b.pdf").status == FAILED
    assert state.get("c.pdf").status not in (DONE, FAILED)


# ---- CLI smoke test ------------------------------------------------------

def test_cli_bulk_ingest_then_bulk_import(tmp_path, monkeypatch):
    """End-to-end: bulk-ingest then bulk-import via the CLI."""
    db_path = tmp_path / "test.db"
    src = tmp_path / "in"
    bundles = tmp_path / "out"
    monkeypatch.setenv("MAGSEARCH_DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MAGSEARCH_BUNDLES_DIR", str(bundles))

    _make_collection(src, [("a.pdf", 1), ("b.pdf", 1)])
    (src / "metadata.csv").write_text(
        "filename,title,date\n"
        "a.pdf,Alpha,1985-01-01\n"
        "b.pdf,Beta,1985-02-01\n"
    )

    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    r = runner.invoke(app, [
        "bulk-ingest", str(src),
        "--bundles-dir", str(bundles),
        "--fake-ocr",
    ])
    assert r.exit_code == 0, r.stdout
    assert (bundles / "alpha-1985-01" / "manifest.json").exists()
    assert (bundles / "beta-1985-02" / "manifest.json").exists()

    r = runner.invoke(app, ["bulk-import", str(bundles)])
    assert r.exit_code == 0, r.stdout
    # Re-run is idempotent.
    r = runner.invoke(app, ["bulk-import", str(bundles)])
    assert r.exit_code == 0, r.stdout
