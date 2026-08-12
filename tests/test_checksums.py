from pathlib import Path

import pytest

from magsearch.checksums import ChecksumError, collect, verify
from magsearch.manifest import Manifest
from tests.fixtures.bundles import make_bundle


def _manifest(bundle: Path) -> Manifest:
    return Manifest.model_validate_json((bundle / "manifest.json").read_text())


def test_collect_covers_every_file_except_the_manifest(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)

    paths = {c.path for c in collect(bundle)}

    on_disk = {
        str(p.relative_to(bundle))
        for p in bundle.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    assert paths == on_disk


def test_collect_is_sorted_for_determinism(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)

    paths = [c.path for c in collect(bundle)]

    assert paths == sorted(paths)


def test_verify_accepts_an_untouched_bundle(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)

    verify(bundle, _manifest(bundle))  # must not raise


def test_verify_rejects_a_modified_file(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)
    (bundle / "pages" / "0001.webp").write_bytes(b"corrupted")

    with pytest.raises(ChecksumError, match="checksum mismatch on pages/0001.webp"):
        verify(bundle, _manifest(bundle))


def test_verify_rejects_a_missing_file(tmp_path):
    bundle = make_bundle(tmp_path, num_pages=2)
    (bundle / "thumbs" / "0002.webp").unlink()

    with pytest.raises(ChecksumError, match="bundle missing file thumbs/0002.webp"):
        verify(bundle, _manifest(bundle))


def test_importer_alias_still_raises_import_error(tmp_path):
    """docs/datamodel/bundles.md tells third-party producers to call this."""
    from magsearch.importer import ImportError as MagImportError
    from magsearch.importer import _verify_checksums

    bundle = make_bundle(tmp_path, num_pages=2)
    (bundle / "pages" / "0001.webp").write_bytes(b"corrupted")

    with pytest.raises(MagImportError, match="checksum mismatch"):
        _verify_checksums(bundle, _manifest(bundle))
