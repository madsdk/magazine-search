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
