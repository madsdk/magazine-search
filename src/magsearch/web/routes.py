from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from magsearch.models import Magazine
from magsearch.web.deps import get_db

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def landing(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    recent = db.scalars(
        select(Magazine).order_by(Magazine.ingested_at.desc()).limit(6)
    ).all()
    return _TEMPLATES.TemplateResponse(request, "index.html", {"recent": recent})
