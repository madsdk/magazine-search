from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import fitz  # PyMuPDF
from PIL import Image

Format = Literal["pdf", "cbz", "cbr"]

_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"
_RAR4_MAGIC = b"Rar!\x1a\x07\x00"
_RAR5_MAGIC = b"Rar!\x1a\x07\x01\x00"


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


def read_pages(path: Path, fmt: Format) -> Iterator[tuple[int, Image.Image]]:
    if fmt == "pdf":
        yield from _read_pdf(path)
    else:
        raise NotImplementedError(f"format {fmt} not yet implemented")


def _read_pdf(path: Path) -> Iterator[tuple[int, Image.Image]]:
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=200)
            mode = "RGB" if pix.alpha == 0 else "RGBA"
            img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
            yield i, img
