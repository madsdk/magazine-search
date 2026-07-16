import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from alembic import command
from alembic.config import Config
from PIL import Image
from sqlalchemy import select

from magsearch.bulk_import import bulk_import
from magsearch.db import make_engine, make_session_factory, session_scope
from magsearch.importer import (
    ImportError as MagImportError,
    delete_bundle_dir,
    import_bundle,
    resolve_magazines,
)
from magsearch.models import Magazine, User
from magsearch.settings import get_settings
from magsearch.web.auth import hash_password, normalize_username

# magsearch.ingest.{pipeline,bulk} transitively import fitz (pymupdf) and
# rarfile, which are only listed in the optional [ingest] extra. Deferred
# imports inside ingest_cmd / bulk_ingest_cmd / _build_ocr_engine keep
# `magsearch web`, `magsearch desktop`, `magsearch import`, etc. working
# on a base install.

app = typer.Typer(no_args_is_help=True, help="Magazine search.")
db_app = typer.Typer(no_args_is_help=True, help="Database commands.")
admin_app = typer.Typer(no_args_is_help=True, help="Admin user management.")
app.add_typer(db_app, name="db")
app.add_typer(admin_app, name="admin")


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


def _build_ocr_engine(*, fake_ocr: bool, device: str, verbose: bool):
    """Construct the OCR engine the ingest/bulk-ingest commands feed to IngestPipeline.

    Centralized so that the single-file and bulk-ingest commands share device
    handling — keep them in lockstep instead of forking the GPU-detection logic.
    """
    if fake_ocr:
        from magsearch.ingest.ocr import FakeOCREngine
        return FakeOCREngine()
    from magsearch.ingest.ocr import PaddleOCREngine
    device_norm = device.lower()
    if device_norm not in {"auto", "cpu", "gpu"}:
        typer.echo(f"--device must be auto, cpu, or gpu (got {device!r})", err=True)
        raise typer.Exit(code=2)
    use_gpu = None if device_norm == "auto" else (device_norm == "gpu")
    return PaddleOCREngine(use_gpu=use_gpu, verbose=verbose)


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
    from magsearch.ingest.pipeline import IngestOptions, IngestPipeline

    settings = get_settings()
    target_bundles = bundles_dir or settings.bundles_dir
    parsed_date = date.fromisoformat(publication_date) if publication_date else None
    engine = _build_ocr_engine(fake_ocr=fake_ocr, device=device, verbose=verbose)
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


@app.command("bulk-ingest")
def bulk_ingest_cmd(
    input_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    sidecar: Path | None = typer.Option(
        None, "--sidecar",
        help="CSV or JSON sidecar with per-file metadata. "
             "Defaults to <input-dir>/metadata.csv (then metadata.json).",
    ),
    state_file: Path | None = typer.Option(
        None, "--state-file",
        help="Per-file state log (JSONL). Default: <input-dir>/.bulk-state.jsonl",
    ),
    bundles_dir: Path | None = typer.Option(None, "--bundles-dir"),
    recursive: bool = typer.Option(False, "--recursive/--no-recursive"),
    retry_failed: bool = typer.Option(
        False, "--retry-failed",
        help="Re-attempt files that previously ended in 'failed'.",
    ),
    force_all: bool = typer.Option(
        False, "--force-all",
        help="Pass force=True to every per-file ingest (overwrite matching bundles).",
    ),
    strict_sidecar: bool = typer.Option(
        False, "--strict-sidecar",
        help="Error out when files have no sidecar row (default: warn).",
    ),
    halt_on_error: bool = typer.Option(
        False, "--halt-on-error",
        help="Stop the batch on the first failure (default: record and continue).",
    ),
    dry_run: bool = typer.Option(False, "--dry-run"),
    fake_ocr: bool = typer.Option(
        False, "--fake-ocr", help="Use FakeOCREngine (tests / dry runs).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    device: str = typer.Option("auto", "--device"),
) -> None:
    """Ingest every PDF/CBR/CBZ in a directory, with resumable per-file state."""
    from magsearch.ingest.bulk import SidecarError, bulk_ingest

    settings = get_settings()
    target_bundles = bundles_dir or settings.bundles_dir
    engine = _build_ocr_engine(fake_ocr=fake_ocr, device=device, verbose=verbose)

    on_page = _make_progress_reporter()

    def on_file_start(i: int, n: int, p: Path) -> None:
        typer.echo(f"[{i}/{n}] {p.name}", err=True)

    def on_file_end(i: int, n: int, p: Path, entry) -> None:
        if on_page is not None:
            typer.echo("", err=True)  # finish the in-place per-page line
        if entry.status == "done":
            typer.echo(f"  → done: {entry.bundle_id}", err=True)
        elif entry.status == "failed":
            typer.echo(f"  → FAILED: {entry.error}", err=True)
        else:  # skipped (already done or previously failed without --retry-failed)
            label = "skipped (already done)" if entry.status == "done" else f"skipped ({entry.status})"
            typer.echo(f"  → {label}", err=True)

    def on_warning(msg: str) -> None:
        typer.echo(f"[warn] {msg}", err=True)

    try:
        result = bulk_ingest(
            input_dir=input_dir,
            sidecar_path=sidecar,
            state_path=state_file,
            bundles_dir=target_bundles,
            ocr_engine=engine,
            recursive=recursive,
            strict_sidecar=strict_sidecar,
            retry_failed=retry_failed,
            force_all=force_all,
            halt_on_error=halt_on_error,
            dry_run=dry_run,
            on_warning=on_warning,
            on_file_start=on_file_start,
            on_file_end=on_file_end,
            on_page=on_page,
        )
    except SidecarError as exc:
        typer.echo(f"sidecar error: {exc}", err=True)
        raise typer.Exit(code=2)
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)

    typer.echo(
        f"bulk-ingest: {result.succeeded} done, {result.failed} failed, "
        f"{result.skipped} skipped — state at {result.state_file}",
    )
    if result.aborted:
        typer.echo(
            "bulk-ingest ABORTED on an unrecoverable OCR error — remaining "
            "files were not processed. Restart the process and re-run with "
            "--retry-failed to resume.",
            err=True,
        )
        raise typer.Exit(code=1)
    if result.failed and not halt_on_error:
        raise typer.Exit(code=1)


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


def _bundle_dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


@app.command("delete")
def delete_cmd(
    ids: Annotated[list[str] | None, typer.Argument(help="Magazine IDs to delete.")] = None,
    title: Annotated[str | None, typer.Option("--title", help="Also delete every issue with this exact title (case-insensitive).")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete magazines: DB rows, search index, and on-disk bundle files."""
    ids = ids or []
    if not ids and not title:
        typer.echo("delete: specify at least one magazine ID or --title", err=True)
        raise typer.Exit(code=2)

    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)

    deleted_ids: list[str] = []
    total_pages = 0
    total_bytes = 0
    with session_scope(factory) as s:
        targets = resolve_magazines(s, ids, title)
        for missing in targets.not_found:
            typer.echo(f"  ! not found: {missing}", err=True)
        if not targets.found:
            typer.echo("no matching magazines to delete", err=True)
            raise typer.Exit(code=1)

        rows = []
        for mag in targets.found:
            size = _bundle_dir_size(settings.bundles_dir / mag.id)
            total_pages += mag.page_count
            total_bytes += size
            issue = f" №{mag.issue}" if mag.issue else ""
            rows.append(f"  {mag.id} — {mag.title}{issue} ({mag.page_count} pages, {_mb(size)})")

        typer.echo(f"Will delete {len(targets.found)} magazine(s), {total_pages} pages, {_mb(total_bytes)}:")
        for line in rows:
            typer.echo(line)

        if not yes:
            typer.confirm("Proceed?", abort=True)

        deleted_ids = [m.id for m in targets.found]
        for mag in targets.found:
            s.delete(mag)
    # session committed here (DB-first); now remove files.
    for mid in deleted_ids:
        delete_bundle_dir(settings.bundles_dir, mid)

    typer.echo(f"deleted {len(deleted_ids)} magazine(s), {total_pages} pages, freed ~{_mb(total_bytes)}")


@app.command("bulk-import")
def bulk_import_cmd(
    bundles_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    state_file: Path | None = typer.Option(
        None, "--state-file",
        help="Per-bundle state log (JSONL). Default: <bundles-dir>/.bulk-import-state.jsonl",
    ),
    retry_failed: bool = typer.Option(False, "--retry-failed"),
    halt_on_error: bool = typer.Option(False, "--halt-on-error"),
) -> None:
    """Import every bundle subdirectory under <bundles-dir> into the database."""
    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)

    def on_bundle_start(i: int, n: int, p: Path) -> None:
        typer.echo(f"[{i}/{n}] {p.name}", err=True)

    def on_bundle_end(i: int, n: int, p: Path, entry) -> None:
        if entry.status == "done":
            typer.echo(f"  → done: {entry.magazine_id}", err=True)
        elif entry.status == "failed":
            typer.echo(f"  → FAILED: {entry.error}", err=True)
        else:
            typer.echo(f"  → skipped ({entry.status})", err=True)

    try:
        result = bulk_import(
            bundles_dir=bundles_dir,
            state_path=state_file,
            session_factory=factory,
            retry_failed=retry_failed,
            halt_on_error=halt_on_error,
            on_bundle_start=on_bundle_start,
            on_bundle_end=on_bundle_end,
        )
    except FileNotFoundError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)

    typer.echo(
        f"bulk-import: {result.succeeded} done, {result.failed} failed, "
        f"{result.skipped} skipped — state at {result.state_file}",
    )
    if result.failed and not halt_on_error:
        raise typer.Exit(code=1)


@app.command("ocr-rescale")
def ocr_rescale_cmd(
    bundles_dir: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    dry_run: bool = typer.Option(False, "--dry-run", help="Report planned changes without rewriting OCR JSONs."),
) -> None:
    """Rescale OCR bboxes in existing bundles to displayed-image pixel coords.

    Older bundles wrote bboxes in original-page pixel coordinates (pre-resize),
    but `pages/<NNNN>.webp` is the downscaled version. The page viewer's
    search-term highlight overlay assumes bboxes are in displayed-image coords.
    This command re-opens each bundle's `original.<ext>`, computes the scale
    between original and displayed sizes per page, and rewrites the OCR JSONs
    in the new self-describing format: {"width", "height", "regions": [...]}.

    Idempotent via shape: array-shaped JSON is treated as old-format and
    rescaled; dict-shaped JSON is treated as already-rescaled and left alone.
    No OCR re-run.
    """
    import json as _json
    from magsearch.ingest.formats import detect_format, read_pages

    bundles = sorted(p for p in bundles_dir.iterdir() if p.is_dir() and (p / "manifest.json").exists())
    if not bundles:
        typer.echo(f"No bundles with manifest.json under {bundles_dir}", err=True)
        raise typer.Exit(code=2)

    rescaled_bundles = 0
    skipped_bundles = 0
    failed_bundles = 0
    for bundle in bundles:
        originals = list(bundle.glob("original.*"))
        if not originals:
            typer.echo(f"  ! {bundle.name}: no original.<ext> — skipping", err=True)
            failed_bundles += 1
            continue
        original = originals[0]
        try:
            fmt = detect_format(original)
        except Exception as exc:
            typer.echo(f"  ! {bundle.name}: detect_format failed: {exc} — skipping", err=True)
            failed_bundles += 1
            continue

        # Pre-flight: how many OCR JSONs are still in old (array) format?
        all_ocr = sorted((bundle / "ocr").glob("*.json"))
        pending_pages = set()
        for ocr_path in all_ocr:
            try:
                data = _json.loads(ocr_path.read_text())
            except Exception:
                continue
            if isinstance(data, list):
                pending_pages.add(ocr_path.stem)

        if not pending_pages:
            typer.echo(f"  · {bundle.name}: all {len(all_ocr)} pages already in new format")
            skipped_bundles += 1
            continue

        pages_rescaled = 0
        pages_unhandled = 0
        # Reset per-bundle so the exception reporter below can't print a
        # page number left over from a previous bundle's iteration.
        current_page_num: int | None = None
        try:
            seen_page_nums = set()
            for page_num, src_image in read_pages(original, fmt):
                current_page_num = page_num
                stem = f"{page_num:04d}"
                seen_page_nums.add(stem)
                if stem not in pending_pages:
                    continue
                page_img = bundle / "pages" / f"{stem}.webp"
                ocr_json = bundle / "ocr" / f"{stem}.json"
                if not (page_img.exists() and ocr_json.exists()):
                    continue
                with Image.open(page_img) as disp:
                    disp_w, disp_h = disp.size
                src_w, src_h = src_image.size
                regions = _json.loads(ocr_json.read_text())
                if not isinstance(regions, list):
                    continue  # already migrated since pre-flight (shouldn't happen)
                sx = disp_w / src_w
                sy = disp_h / src_h
                new_doc = {
                    "width": disp_w,
                    "height": disp_h,
                    "regions": [
                        {
                            "text": r["text"],
                            "bbox": [r["bbox"][0] * sx, r["bbox"][1] * sy, r["bbox"][2] * sx, r["bbox"][3] * sy],
                            "confidence": r["confidence"],
                        }
                        for r in regions
                    ],
                }
                if not dry_run:
                    ocr_json.write_text(_json.dumps(new_doc))
                pages_rescaled += 1

            # Pages in the bundle but missing from read_pages: leave alone.
            pages_unhandled = len(pending_pages - seen_page_nums)
        except Exception as exc:
            page_ctx = current_page_num if current_page_num is not None else "?"
            typer.echo(f"  ! {bundle.name}: read failed on page {page_ctx}: {exc}", err=True)
            failed_bundles += 1
            continue

        suffix = " (dry-run)" if dry_run else ""
        if pages_unhandled:
            typer.echo(
                f"  ! {bundle.name}: {pages_rescaled} pages rescaled{suffix}, "
                f"{pages_unhandled} pages in bundle not present in original.<ext> — left untouched"
            )
        else:
            typer.echo(f"  ✓ {bundle.name}: {pages_rescaled} pages rescaled{suffix}")
        if pages_rescaled:
            rescaled_bundles += 1

    typer.echo(
        f"ocr-rescale: {rescaled_bundles} bundles rewritten, "
        f"{skipped_bundles} already in new format, {failed_bundles} failed",
    )
    if failed_bundles:
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


@app.command("desktop")
def desktop_cmd() -> None:
    """Launch magsearch as a desktop app (pywebview)."""
    # Deferred import: pywebview is only required for the desktop subcommand.
    from magsearch.desktop import main as desktop_main
    desktop_main()


def _open_session():
    settings = get_settings()
    return make_session_factory(make_engine(settings.database_url))


@admin_app.command("create")
def admin_create(
    username: Annotated[str, typer.Argument(help="Username for the new admin.")],
    password: Annotated[
        str,
        typer.Option(
            "--password",
            prompt=True,
            confirmation_prompt=True,
            hide_input=True,
            help="Password (prompted if not given).",
        ),
    ],
) -> None:
    """Create a new administrator account."""
    factory = _open_session()
    normalized = normalize_username(username)
    if not normalized:
        typer.echo("username must not be empty", err=True)
        raise typer.Exit(code=2)
    with session_scope(factory) as s:
        if s.scalar(select(User).where(User.username == normalized)):
            typer.echo(f"user '{normalized}' already exists", err=True)
            raise typer.Exit(code=1)
        s.add(User(
            username=normalized,
            password_hash=hash_password(password),
            is_admin=True,
            created_at=datetime.utcnow(),
            last_login_at=None,
        ))
    typer.echo(f"admin user '{normalized}' created")


@admin_app.command("list")
def admin_list() -> None:
    """List all users."""
    factory = _open_session()
    with session_scope(factory) as s:
        users = s.scalars(select(User).order_by(User.username)).all()
        if not users:
            typer.echo("(no users)")
            return
        for u in users:
            flag = "admin" if u.is_admin else "user"
            typer.echo(f"{u.username}\t{flag}")


@admin_app.command("reset-password")
def admin_reset_password(
    username: Annotated[str, typer.Argument()],
    password: Annotated[
        str,
        typer.Option(
            "--password",
            prompt=True,
            confirmation_prompt=True,
            hide_input=True,
        ),
    ],
) -> None:
    """Reset a user's password from the CLI."""
    factory = _open_session()
    normalized = normalize_username(username)
    with session_scope(factory) as s:
        user = s.scalar(select(User).where(User.username == normalized))
        if user is None:
            typer.echo(f"user '{normalized}' not found", err=True)
            raise typer.Exit(code=1)
        user.password_hash = hash_password(password)
    typer.echo(f"password reset for '{normalized}'")
