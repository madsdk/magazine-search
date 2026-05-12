from datetime import date

from magsearch.ingest.ids import slugify, content_hash, generate_id


def test_slugify_basic():
    assert slugify("Byte Magazine") == "byte-magazine"


def test_slugify_unicode_and_punctuation():
    assert slugify("Père Ubu — Issue #3!") == "pere-ubu-issue-3"


def test_slugify_empty_returns_empty_string():
    assert slugify("") == ""
    assert slugify("---") == ""


def test_content_hash_deterministic(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"hello")
    h1 = content_hash(p)
    h2 = content_hash(p)
    assert h1 == h2
    assert len(h1) == 64


def test_generate_id_uses_title_and_date():
    h = "a" * 64
    assert generate_id(title="Byte", publication_date=date(1985, 12, 1),
                       override=None, content_hash=h) == "byte-1985-12"


def test_generate_id_override_wins():
    h = "a" * 64
    assert generate_id(title="Byte", publication_date=date(1985, 12, 1),
                       override="custom-id", content_hash=h) == "custom-id"


def test_generate_id_falls_back_to_hash():
    h = "abcdef0123456789" + "0" * 48
    assert generate_id(title="Untitled", publication_date=None,
                       override=None, content_hash=h) == "abcdef012345"
