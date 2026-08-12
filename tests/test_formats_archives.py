import pytest
from PIL import Image

from magsearch.ingest.formats import MissingRarBackendError, detect_format, read_pages
from tests.fixtures.cbrs import make_cbr
from tests.fixtures.cbzs import make_cbz


def test_detect_format_cbz(tmp_path):
    p = make_cbz(tmp_path / "a.cbz")
    assert detect_format(p) == "cbz"


def test_read_pages_cbz_yields_in_name_order(tmp_path):
    p = make_cbz(tmp_path / "a.cbz", num_pages=3)
    pages = list(read_pages(p, "cbz"))
    assert [pn for pn, _ in pages] == [1, 2, 3]
    assert all(isinstance(img, Image.Image) for _, img in pages)


def test_detect_format_cbr_synthetic(tmp_path):
    """Detection by magic bytes only — no real RAR archive needed."""
    p = tmp_path / "fake.cbr"
    p.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 100)
    assert detect_format(p) == "cbr"


def test_read_pages_cbr_yields_in_name_order(tmp_path):
    """Covers the CBR branch of read_pages, not rarfile's external backend.

    make_cbr stores its entries uncompressed, and rarfile reads stored,
    unencrypted entries in pure Python (DirectReader) — no unar/unrar
    involved. Real-world CBRs are compressed and *do* need a backend on PATH;
    that the images ship a working one is covered by test_dockerfiles.py, since
    asserting it here would need a compressed fixture, which in turn needs a
    RAR compressor we deliberately don't depend on.
    """
    p = make_cbr(tmp_path / "a.cbr", num_pages=3)
    pages = list(read_pages(p, "cbr"))
    assert [pn for pn, _ in pages] == [1, 2, 3]
    assert all(isinstance(img, Image.Image) for _, img in pages)


def test_read_pages_cbr_without_backend_names_the_missing_tool(tmp_path, no_rar_backend):
    """rarfile's own "Cannot find working tool" doesn't say what to install."""
    p = make_cbr(tmp_path / "a.cbr")
    with pytest.raises(MissingRarBackendError) as excinfo:
        list(read_pages(p, "cbr"))
    message = str(excinfo.value)
    assert "unar" in message
    assert "unrar" in message
    assert str(p) in message
