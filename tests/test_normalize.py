from pathlib import Path

from PIL import Image

from magsearch.ingest.normalize import encode_page, encode_thumb, write_cover


def test_encode_page_resizes_long_edge_and_writes_webp(tmp_path):
    src = Image.new("RGB", (3600, 2400), "white")
    out = tmp_path / "page.webp"
    encode_page(src, out)
    assert out.exists()
    img = Image.open(out)
    assert img.format == "WEBP"
    assert max(img.size) == 1800
    assert img.size == (1800, 1200)


def test_encode_page_skips_upscale(tmp_path):
    src = Image.new("RGB", (500, 400), "white")
    out = tmp_path / "page.webp"
    encode_page(src, out)
    img = Image.open(out)
    assert img.size == (500, 400)


def test_encode_thumb_long_edge_400(tmp_path):
    src = Image.new("RGB", (1000, 1500), "white")
    out = tmp_path / "thumb.webp"
    encode_thumb(src, out)
    img = Image.open(out)
    assert max(img.size) == 400


def test_write_cover_copies_thumb(tmp_path):
    thumb = tmp_path / "0001.webp"
    Image.new("RGB", (200, 300), "red").save(thumb, "WEBP")
    cover = tmp_path / "cover.webp"
    write_cover(thumb, cover)
    assert cover.exists()
    assert cover.read_bytes() == thumb.read_bytes()
