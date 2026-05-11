from PIL import Image

from magsearch.ingest.ocr import (
    FakeOCREngine,
    OCRRegion,
    concatenate_reading_order,
)


def test_ocr_region_fields():
    r = OCRRegion(text="hello", bbox=(0, 0, 100, 20), confidence=0.95)
    assert r.text == "hello"
    assert r.bbox == (0, 0, 100, 20)


def test_fake_engine_returns_default():
    e = FakeOCREngine()
    img = Image.new("RGB", (50, 50), "white")
    out = e.recognize(img)
    assert len(out) == 1
    assert out[0].text == "fake page"


def test_fake_engine_uses_scripted_responses_in_order():
    scripted = [
        [OCRRegion(text="one", bbox=(0, 0, 10, 10), confidence=1.0)],
        [OCRRegion(text="two", bbox=(0, 0, 10, 10), confidence=1.0)],
    ]
    e = FakeOCREngine(responses=scripted)
    img = Image.new("RGB", (50, 50), "white")
    assert e.recognize(img)[0].text == "one"
    assert e.recognize(img)[0].text == "two"
    assert e.recognize(img)[0].text == "fake page"  # falls back to default


def test_concatenate_reading_order_sorts_top_to_bottom_then_left_to_right():
    regions = [
        OCRRegion(text="C", bbox=(0, 200, 50, 220), confidence=1.0),
        OCRRegion(text="A", bbox=(0, 0, 50, 20), confidence=1.0),
        OCRRegion(text="B", bbox=(100, 0, 150, 20), confidence=1.0),
    ]
    assert concatenate_reading_order(regions) == "A B C"


def test_concatenate_reading_order_groups_by_line_tolerance():
    # Two regions almost at the same y should be treated as one line and ordered by x.
    regions = [
        OCRRegion(text="right", bbox=(200, 100, 250, 120), confidence=1.0),
        OCRRegion(text="left", bbox=(0, 102, 50, 122), confidence=1.0),
    ]
    assert concatenate_reading_order(regions) == "left right"
