import shutil
from pathlib import Path

from PIL import Image

PAGE_LONG_EDGE = 1800
PAGE_QUALITY = 80
THUMB_LONG_EDGE = 400
THUMB_QUALITY = 70


def _resize_long_edge(img: Image.Image, long_edge: int) -> Image.Image:
    w, h = img.size
    longest = max(w, h)
    if longest <= long_edge:
        return img
    scale = long_edge / longest
    return img.resize((int(round(w * scale)), int(round(h * scale))), Image.LANCZOS)


def encode_page(image: Image.Image, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    resized = _resize_long_edge(image.convert("RGB"), PAGE_LONG_EDGE)
    resized.save(out, "WEBP", quality=PAGE_QUALITY)


def encode_thumb(image: Image.Image, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    resized = _resize_long_edge(image.convert("RGB"), THUMB_LONG_EDGE)
    resized.save(out, "WEBP", quality=THUMB_QUALITY)


def write_cover(first_thumb: Path, cover: Path) -> None:
    cover.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(first_thumb, cover)
