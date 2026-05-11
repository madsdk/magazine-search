import shutil
from pathlib import Path

import pytest
from PIL import Image

from magsearch.ingest.formats import detect_format, read_pages
from tests.fixtures.cbzs import make_cbz

CBR_FIXTURE = Path(__file__).parent / "fixtures" / "tiny.cbr"


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


@pytest.mark.skipif(
    not CBR_FIXTURE.exists(),
    reason="tiny.cbr fixture not present — see tests/fixtures/cbzs.py docstring",
)
@pytest.mark.skipif(
    shutil.which("unrar") is None and shutil.which("unar") is None,
    reason="needs unrar or unar on PATH",
)
def test_read_pages_cbr():
    pages = list(read_pages(CBR_FIXTURE, "cbr"))
    assert len(pages) >= 1
    assert all(isinstance(img, Image.Image) for _, img in pages)
