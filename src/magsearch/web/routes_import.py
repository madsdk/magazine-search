"""Public /import endpoint: accept one or more .magbundle files and
register each into the local database.

Registered only in desktop mode (settings.auth_enabled is False). The web
deployment continues to use the admin-only single-file upload at
/admin/issues/upload.
"""

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from magsearch.importer import ImportError as MagImportError
from magsearch.importer import import_bundle
from magsearch.settings import Settings, get_settings
from magsearch.web.bundle_upload import BundleUploadError, extract_and_stage
from magsearch.web.deps import get_db

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@dataclass
class _ImportOutcome:
    filename: str
    status: str  # "imported" | "failed"
    magazine_id: str | None
    error: str | None


@router.get("/import", response_class=HTMLResponse)
def import_form(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "import.html", {})


@router.post("/import", response_class=HTMLResponse)
def import_submit(
    request: Request,
    bundles: list[UploadFile],
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    outcomes: list[_ImportOutcome] = []
    for upload in bundles:
        outcomes.append(_import_one(upload, db, settings))
    return _TEMPLATES.TemplateResponse(
        request, "import_result.html", {"outcomes": outcomes}
    )


def _import_one(
    upload: UploadFile, db: Session, settings: Settings
) -> _ImportOutcome:
    filename = upload.filename or "(unnamed)"
    tmp_dir = Path(tempfile.mkdtemp(prefix="magsearch-import-"))
    tmp_zip = tmp_dir / "upload.zip"
    try:
        cap = settings.max_upload_bytes
        bytes_written = 0
        with tmp_zip.open("wb") as out:
            while True:
                chunk = upload.file.read(64 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > cap:
                    return _ImportOutcome(
                        filename=filename,
                        status="failed",
                        magazine_id=None,
                        error=f"file exceeds {cap // (1024 * 1024)} MB cap",
                    )
                out.write(chunk)

        try:
            staged = extract_and_stage(
                tmp_zip,
                settings.bundles_dir,
                max_uncompressed_bytes=settings.max_upload_bytes,
            )
        except BundleUploadError as exc:
            return _ImportOutcome(
                filename=filename, status="failed",
                magazine_id=None, error=str(exc),
            )

        try:
            magazine_id = import_bundle(staged, db)
        except MagImportError as exc:
            db.rollback()
            return _ImportOutcome(
                filename=filename, status="failed",
                magazine_id=None, error=f"import failed: {exc}",
            )

        db.commit()
        return _ImportOutcome(
            filename=filename, status="imported",
            magazine_id=magazine_id, error=None,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
