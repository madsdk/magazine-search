from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict


class FileChecksum(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str


class PageEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_number: int
    image_path: str
    thumb_path: str
    ocr_path: str
    text: str


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    id: str
    title: str
    issue: str | None = None
    publication_date: date | None = None
    publisher: str | None = None
    original_filename: str
    original_format: Literal["pdf", "cbz", "cbr"]
    page_count: int
    content_hash: str
    ocr_engine: str
    ocr_engine_version: str
    cover_path: str
    pages: list[PageEntry]
    checksums: list[FileChecksum]
