import sys
import time
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from alembic import command
from alembic.config import Config

from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.importer import ImportError as MagImportError, import_bundle
from magsearch.ingest.ocr import FakeOCREngine
from magsearch.ingest.pipeline import IngestOptions, IngestPipeline
from magsearch.settings import get_settings

app = typer.Typer(no_args_is_help=True, help="Magazine search.")
db_app = typer.Typer(no_args_is_help=True, help="Database commands.")
app.add_typer(db_app, name="db")


def _make_progress_reporter():
    """Return a callback that overwrites a single stderr line with `page X/N`,
    or None when stderr isn't a TTY (so piped logs aren't spammed with \\r)."""
    if not sys.stderr.isatty():
        return None
    width = [0]

    def report(current: int, total: int) -> None:
        msg = f"page {current}/{total}"
        pad = max(0, width[0] - len(msg))
        sys.stderr.write("\r" + msg + " " * pad)
        sys.stderr.flush()
        width[0] = len(msg)

    return report


def _alembic_cfg() -> Config:
    settings = get_settings()
    here = Path(__file__).resolve().parent
    candidates = [
        here.parent.parent / "alembic.ini",  # editable install from source checkout
        Path.cwd() / "alembic.ini",          # working directory (docker WORKDIR)
    ]
    for path in candidates:
        if path.is_file():
            cfg = Config(str(path))
            cfg.set_main_option("sqlalchemy.url", settings.database_url)
            return cfg
    raise FileNotFoundError(
        "alembic.ini not found in: " + ", ".join(str(p) for p in candidates)
    )


@app.command("ingest")
def ingest_cmd(
    source: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    title: str = typer.Option("", "--title"),
    issue: str | None = typer.Option(None, "--issue"),
    publication_date: str | None = typer.Option(None, "--date"),
    publisher: str | None = typer.Option(None, "--publisher"),
    id_override: str | None = typer.Option(None, "--id"),
    bundles_dir: Path | None = typer.Option(None, "--bundles-dir"),
    force: bool = typer.Option(False, "--force"),
    fake_ocr: bool = typer.Option(
        False, "--fake-ocr", help="Use FakeOCREngine (tests / dry runs)."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show paddle/paddleocr init logs and warnings."
    ),
    device: str = typer.Option(
        "auto", "--device",
        help="OCR device: auto (detect), cpu, or gpu. GPU requires paddlepaddle-gpu.",
    ),
) -> None:
    settings = get_settings()
    target_bundles = bundles_dir or settings.bundles_dir
    parsed_date = date.fromisoformat(publication_date) if publication_date else None
    if fake_ocr:
        engine = FakeOCREngine()
    else:
        from magsearch.ingest.ocr import PaddleOCREngine
        device_norm = device.lower()
        if device_norm not in {"auto", "cpu", "gpu"}:
            typer.echo(f"--device must be auto, cpu, or gpu (got {device!r})", err=True)
            raise typer.Exit(code=2)
        use_gpu = None if device_norm == "auto" else (device_norm == "gpu")
        engine = PaddleOCREngine(use_gpu=use_gpu, verbose=verbose)
    pipeline = IngestPipeline(
        bundles_root=target_bundles,
        ocr_engine=engine,
        options=IngestOptions(
            title=title, issue=issue,
            publication_date=parsed_date, publisher=publisher,
            id_override=id_override,
        ),
    )
    on_page = _make_progress_reporter()
    started = time.monotonic()
    result = pipeline.run(source, force=force, on_page=on_page)
    if on_page is not None:
        typer.echo("", err=True)  # finish the in-place progress line
    elapsed = int(time.monotonic() - started)
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    typer.echo(
        f"ingested {result.id} → {result.bundle_dir} in {hours:02d}:{minutes:02d}:{seconds:02d}"
    )


@app.command("import")
def import_cmd(bundle: Annotated[Path, typer.Argument(exists=True, file_okay=False)]) -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as s:
            mag_id = import_bundle(bundle, s)
        typer.echo(f"imported {mag_id}")
    except MagImportError as exc:
        typer.echo(f"import failed: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command("web")
def web_cmd(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    reload: bool = typer.Option(False, "--reload"),
) -> None:
    uvicorn.run("magsearch.web.app:app", host=host, port=port, reload=reload)


@db_app.command("upgrade")
def db_upgrade(revision: str = typer.Argument("head")) -> None:
    command.upgrade(_alembic_cfg(), revision)


@db_app.command("downgrade")
def db_downgrade(revision: str = typer.Argument("-1")) -> None:
    command.downgrade(_alembic_cfg(), revision)
