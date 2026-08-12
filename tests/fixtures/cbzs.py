"""CBZ generation helper for tests.

For CBR fixtures see `cbrs.make_cbr` — it writes a stored-only RAR 4 archive
directly, so no non-free `rar` binary is needed.
"""
import io
import zipfile
from pathlib import Path

from PIL import Image


def make_cbz(path: Path, num_pages: int = 2) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(num_pages):
            img = Image.new("RGB", (100, 150), color=(i * 40, i * 40, i * 40))
            buf = io.BytesIO()
            img.save(buf, "PNG")
            zf.writestr(f"page{i + 1:03d}.png", buf.getvalue())
    return path
