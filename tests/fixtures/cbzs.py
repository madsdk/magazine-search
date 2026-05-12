"""CBZ generation helper for tests.

To generate a CBR fixture later (requires the non-free `rar` binary on PATH):

    mkdir -p /tmp/cbr_src
    python -c "from PIL import Image; Image.new('RGB',(50,50),'red').save('/tmp/cbr_src/001.png'); Image.new('RGB',(50,50),'blue').save('/tmp/cbr_src/002.png')"
    cd /tmp/cbr_src && rar a -m0 tiny.rar 001.png 002.png && mv tiny.rar <repo>/tests/fixtures/tiny.cbr

When `tests/fixtures/tiny.cbr` exists, the CBR-read test is auto-enabled.
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
