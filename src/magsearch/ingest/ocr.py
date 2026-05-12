from dataclasses import dataclass
from typing import Protocol

from PIL import Image


@dataclass(frozen=True)
class OCRRegion:
    text: str
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1
    confidence: float


class OCREngine(Protocol):
    name: str
    version: str

    def recognize(self, image: Image.Image) -> list[OCRRegion]: ...


class FakeOCREngine:
    """Deterministic test double. Returns scripted responses then falls back to default."""

    name: str = "fake"
    version: str = "0.0.1"

    def __init__(self, responses: list[list[OCRRegion]] | None = None) -> None:
        self._responses = list(responses or [])
        self._index = 0
        self._default = [OCRRegion(text="fake page", bbox=(0, 0, 100, 20), confidence=1.0)]

    def recognize(self, image: Image.Image) -> list[OCRRegion]:
        if self._index < len(self._responses):
            r = self._responses[self._index]
            self._index += 1
            return r
        return self._default


_LINE_TOLERANCE_PX = 15


def concatenate_reading_order(regions: list[OCRRegion]) -> str:
    if not regions:
        return ""
    sorted_by_y = sorted(regions, key=lambda r: r.bbox[1])
    lines: list[list[OCRRegion]] = []
    for r in sorted_by_y:
        if lines and abs(r.bbox[1] - lines[-1][0].bbox[1]) <= _LINE_TOLERANCE_PX:
            lines[-1].append(r)
        else:
            lines.append([r])
    parts: list[str] = []
    for line in lines:
        line.sort(key=lambda r: r.bbox[0])
        parts.append(" ".join(r.text for r in line))
    return " ".join(parts)


def _try_import_paddleocr():
    try:
        from paddleocr import PaddleOCR  # type: ignore
        return PaddleOCR
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "PaddleOCR not installed — install with `pip install magsearch[ocr]`"
        ) from exc


class PaddleOCREngine:
    name: str = "paddleocr"

    def __init__(self, lang: str = "en", use_gpu: bool = True) -> None:
        PaddleOCR = _try_import_paddleocr()
        import paddleocr  # type: ignore
        self.version = getattr(paddleocr, "__version__", "unknown")
        self._impl = PaddleOCR(lang=lang, use_gpu=use_gpu, show_log=False)

    def recognize(self, image):
        import numpy as np  # type: ignore
        arr = np.array(image.convert("RGB"))
        raw = self._impl.ocr(arr, cls=True)
        regions: list[OCRRegion] = []
        if not raw or not raw[0]:
            return regions
        for box, (text, confidence) in raw[0]:
            xs = [pt[0] for pt in box]
            ys = [pt[1] for pt in box]
            regions.append(OCRRegion(
                text=text,
                bbox=(min(xs), min(ys), max(xs), max(ys)),
                confidence=float(confidence),
            ))
        return regions
