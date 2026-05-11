from PIL import Image

from magsearch.ingest.formats import detect_format, read_pages
from tests.fixtures.pdfs import make_pdf


def test_detect_format_pdf(tmp_path):
    p = make_pdf(tmp_path / "a.pdf")
    assert detect_format(p) == "pdf"


def test_detect_format_rejects_unknown(tmp_path):
    p = tmp_path / "x.txt"
    p.write_bytes(b"not a real magazine")
    assert detect_format(p) is None


def test_read_pages_pdf_yields_images(tmp_path):
    p = make_pdf(tmp_path / "a.pdf", num_pages=3)
    pages = list(read_pages(p, "pdf"))
    assert [pn for pn, _ in pages] == [1, 2, 3]
    assert all(isinstance(img, Image.Image) for _, img in pages)
    assert all(img.mode in ("RGB", "RGBA") for _, img in pages)
