from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import Literal
from zipfile import ZipFile

import fitz  # PyMuPDF
import rarfile
from PIL import Image

Format = Literal["pdf", "cbz", "cbr"]


class MissingRarBackendError(RuntimeError):
    """No external tool rarfile can drive is installed."""


# rarfile only parses RAR headers itself; decompression is shelled out to one
# of unrar / unar / 7z / bsdtar. Its own error for this is the bare "Cannot
# find working tool", which says nothing about what to install.
_MISSING_RAR_BACKEND = (
    "cannot read {path}: decompressing a CBR needs an external RAR tool on "
    "PATH and none was found. Install `unar` (free) or `unrar` (non-free) — "
    "on Debian/Ubuntu, `apt-get install unar`. `unrar-free` is not a working "
    "substitute: rarfile cannot drive it."
)

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_RAR4_MAGIC = b"Rar!\x1a\x07\x00"
_RAR5_MAGIC = b"Rar!\x1a\x07\x01\x00"

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")


def detect_format(path: Path) -> Format | None:
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(_PDF_MAGIC):
        return "pdf"
    if head.startswith(_ZIP_MAGIC):
        return "cbz"
    if head.startswith(_RAR4_MAGIC) or head.startswith(_RAR5_MAGIC):
        return "cbr"
    return None


def page_count(path: Path, fmt: Format) -> int:
    if fmt == "pdf":
        with fitz.open(str(path)) as doc:
            return len(doc)
    if fmt == "cbz":
        with ZipFile(path) as zf:
            return sum(
                1 for n in zf.namelist()
                if not n.endswith("/") and n.lower().endswith(_IMAGE_EXTS)
            )
    if fmt == "cbr":
        with rarfile.RarFile(path) as rf:
            return sum(
                1 for n in rf.namelist()
                if not n.endswith("/") and n.lower().endswith(_IMAGE_EXTS)
            )
    raise ValueError(f"unsupported format {fmt!r}")


def read_pages(path: Path, fmt: Format) -> Iterator[tuple[int, Image.Image]]:
    if fmt == "pdf":
        yield from _read_pdf(path)
    elif fmt == "cbz":
        yield from _read_cbz(path)
    elif fmt == "cbr":
        yield from _read_cbr(path)
    else:
        raise ValueError(f"unsupported format {fmt!r}")


def _read_pdf(path: Path) -> Iterator[tuple[int, Image.Image]]:
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=200)
            mode = "RGB" if pix.alpha == 0 else "RGBA"
            img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
            yield i, img


def _read_cbz(path: Path) -> Iterator[tuple[int, Image.Image]]:
    with ZipFile(path) as zf:
        names = sorted(
            n for n in zf.namelist()
            if not n.endswith("/") and n.lower().endswith(_IMAGE_EXTS)
        )
        for i, name in enumerate(names, start=1):
            with zf.open(name) as fh:
                img = Image.open(BytesIO(fh.read()))
                img.load()
                yield i, img


def _read_cbr(path: Path) -> Iterator[tuple[int, Image.Image]]:
    try:
        with rarfile.RarFile(path) as rf:
            names = sorted(
                n for n in rf.namelist()
                if not n.endswith("/") and n.lower().endswith(_IMAGE_EXTS)
            )
            for i, name in enumerate(names, start=1):
                with rf.open(name) as fh:
                    img = Image.open(BytesIO(fh.read()))
                    img.load()
                    yield i, img
    except rarfile.RarCannotExec as exc:
        raise MissingRarBackendError(_MISSING_RAR_BACKEND.format(path=path)) from exc
