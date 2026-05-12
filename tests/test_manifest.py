import json
from datetime import date

import pytest
from pydantic import ValidationError

from magsearch.manifest import Manifest, PageEntry, FileChecksum


def _sample_manifest_dict() -> dict:
    return {
        "schema_version": 1,
        "id": "byte-1985-12",
        "title": "Byte",
        "issue": "Vol 10 No 12",
        "publication_date": "1985-12-01",
        "publisher": "McGraw-Hill",
        "original_filename": "byte.pdf",
        "original_format": "pdf",
        "page_count": 2,
        "content_hash": "abc123",
        "ocr_engine": "paddleocr",
        "ocr_engine_version": "2.7.0",
        "cover_path": "cover.webp",
        "pages": [
            {"page_number": 1, "image_path": "pages/0001.webp",
             "thumb_path": "thumbs/0001.webp", "ocr_path": "ocr/0001.json",
             "text": "first page text"},
            {"page_number": 2, "image_path": "pages/0002.webp",
             "thumb_path": "thumbs/0002.webp", "ocr_path": "ocr/0002.json",
             "text": "second page text"},
        ],
        "checksums": [
            {"path": "pages/0001.webp", "sha256": "111"},
            {"path": "cover.webp", "sha256": "222"},
        ],
    }


def test_manifest_roundtrip():
    m = Manifest.model_validate(_sample_manifest_dict())
    assert m.id == "byte-1985-12"
    assert m.publication_date == date(1985, 12, 1)
    assert len(m.pages) == 2
    again = Manifest.model_validate(json.loads(m.model_dump_json()))
    assert again == m


def test_manifest_rejects_wrong_schema_version():
    d = _sample_manifest_dict()
    d["schema_version"] = 99
    with pytest.raises(ValidationError):
        Manifest.model_validate(d)


def test_manifest_rejects_extra_fields():
    d = _sample_manifest_dict()
    d["unexpected"] = "should fail"
    with pytest.raises(ValidationError):
        Manifest.model_validate(d)


def test_manifest_format_must_be_known():
    d = _sample_manifest_dict()
    d["original_format"] = "txt"
    with pytest.raises(ValidationError):
        Manifest.model_validate(d)
