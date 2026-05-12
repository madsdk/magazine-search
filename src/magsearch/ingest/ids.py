import hashlib
import re
import unicodedata
from datetime import date
from pathlib import Path


_SLUG_KEEP = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    normalized = normalized.lower()
    normalized = _SLUG_KEEP.sub("-", normalized).strip("-")
    return normalized


def content_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def generate_id(
    *, title: str, publication_date: date | None, override: str | None, content_hash: str
) -> str:
    if override:
        return slugify(override)
    slug = slugify(title)
    if slug and publication_date is not None:
        return f"{slug}-{publication_date.year:04d}-{publication_date.month:02d}"
    return content_hash[:12]
