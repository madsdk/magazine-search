from PIL import Image

from magsearch.ingest.ocr import (
    FakeOCREngine,
    FatalOCRError,
    OCRRegion,
    concatenate_reading_order,
    _is_unrecoverable_gpu_error,
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


# The exact message paddle surfaces for the CUDA fault seen in bulk ingestion.
_CUDA_ILLEGAL_ADDRESS = (
    "(External) CUDA error(700), an illegal memory access was encountered. "
    "[Hint: 'cudaErrorIllegalAddress'. ... This leaves the process in an "
    "inconsistent state and any further CUDA work will return the same error. "
    "To continue using CUDA, the process must be terminated and relaunched.]"
)


def test_is_unrecoverable_gpu_error_matches_cuda_illegal_address():
    assert _is_unrecoverable_gpu_error(RuntimeError(_CUDA_ILLEGAL_ADDRESS))


def test_is_unrecoverable_gpu_error_ignores_recoverable_failures():
    # Plain OOM is often per-image recoverable — a smaller next page may work,
    # so it must NOT be treated as fatal.
    assert not _is_unrecoverable_gpu_error(RuntimeError("Out of memory error on GPU 0"))
    assert not _is_unrecoverable_gpu_error(ValueError("bad image shape"))


def test_recognize_raises_fatal_and_marks_engine_dead_on_cuda_fault():
    """A sticky GPU fault must surface as FatalOCRError and disable the engine so
    later pages fail fast instead of re-triggering the same crash."""
    from magsearch.ingest.ocr import PaddleOCREngine

    engine = PaddleOCREngine.__new__(PaddleOCREngine)  # skip paddle-loading __init__
    engine._dead = False

    class BoomImpl:
        calls = 0

        def predict(self, arr):
            BoomImpl.calls += 1
            raise RuntimeError(_CUDA_ILLEGAL_ADDRESS)

    engine._impl = BoomImpl()
    img = Image.new("RGB", (20, 20), "white")

    with pytest.raises(FatalOCRError):
        engine.recognize(img)
    assert engine._dead is True
    assert BoomImpl.calls == 1

    # Second call fails fast without touching the (now-poisoned) impl.
    with pytest.raises(FatalOCRError):
        engine.recognize(img)
    assert BoomImpl.calls == 1  # predict was not called again


def test_recognize_reraises_ordinary_errors_untouched():
    from magsearch.ingest.ocr import PaddleOCREngine

    engine = PaddleOCREngine.__new__(PaddleOCREngine)
    engine._dead = False

    class BadImpl:
        def predict(self, arr):
            raise ValueError("some transient decode error")

    engine._impl = BadImpl()
    img = Image.new("RGB", (20, 20), "white")
    with pytest.raises(ValueError):
        engine.recognize(img)
    assert engine._dead is False  # ordinary errors don't disable the engine


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


import pytest
from PIL import ImageDraw, ImageFont


@pytest.mark.ocr
def test_paddleocr_real_engine_reads_simple_text(tmp_path):
    from magsearch.ingest.ocr import PaddleOCREngine

    img = Image.new("RGB", (400, 100), "white")
    d = ImageDraw.Draw(img)
    d.text((20, 30), "HELLO WORLD", fill="black")

    engine = PaddleOCREngine(use_gpu=False)
    regions = engine.recognize(img)
    joined = " ".join(r.text.upper() for r in regions)
    assert "HELLO" in joined
