from datetime import date

from magsearch.ingest.ids import slugify, content_hash, generate_id, resolve_unique_id


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


def _write_fake_manifest(bundles_root, magazine_id, content_hash):
    """Drop a minimally-valid manifest.json into a bundle dir for collision tests."""
    import json
    bundle = bundles_root / magazine_id
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "id": magazine_id, "title": "x",
        "original_filename": "x.pdf", "original_format": "pdf",
        "page_count": 0, "content_hash": content_hash,
        "ocr_engine": "fake", "ocr_engine_version": "0",
        "cover_path": "", "pages": [], "checksums": [],
    }))


def test_resolve_unique_id_free_slot(tmp_path):
    assert resolve_unique_id(
        base_id="byte-1985-12", content_hash="a" * 64,
        issue=None, bundles_root=tmp_path,
    ) == "byte-1985-12"


def test_resolve_unique_id_idempotent_on_matching_hash(tmp_path):
    h = "a" * 64
    _write_fake_manifest(tmp_path, "byte-1985-12", h)
    assert resolve_unique_id(
        base_id="byte-1985-12", content_hash=h,
        issue=None, bundles_root=tmp_path,
    ) == "byte-1985-12"


def test_resolve_unique_id_collision_falls_back_to_hash_suffix(tmp_path):
    _write_fake_manifest(tmp_path, "byte-1985-12", "a" * 64)
    new_hash = "b" * 64
    assert resolve_unique_id(
        base_id="byte-1985-12", content_hash=new_hash,
        issue=None, bundles_root=tmp_path,
    ) == f"byte-1985-12-{new_hash[:6]}"


def test_resolve_unique_id_collision_prefers_issue_slug(tmp_path):
    _write_fake_manifest(tmp_path, "byte-1985-12", "a" * 64)
    new_hash = "b" * 64
    assert resolve_unique_id(
        base_id="byte-1985-12", content_hash=new_hash,
        issue="Christmas Double Issue", bundles_root=tmp_path,
    ) == "byte-1985-12-christmas-double-issue"


def test_resolve_unique_id_double_collision_walks_through_hash_suffixes(tmp_path):
    # Same base id AND same issue slug already taken by something else.
    _write_fake_manifest(tmp_path, "byte-1985-12", "a" * 64)
    _write_fake_manifest(tmp_path, "byte-1985-12-christmas", "c" * 64)
    new_hash = "b" * 64
    assert resolve_unique_id(
        base_id="byte-1985-12", content_hash=new_hash,
        issue="Christmas", bundles_root=tmp_path,
    ) == f"byte-1985-12-{new_hash[:6]}"
