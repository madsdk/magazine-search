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

    def __init__(self, lang: str = "en", use_gpu: bool | None = None) -> None:
        # Disable PIR-based execution. Under PaddlePaddle 3.3.x on CPU the PIR executor
        # raises `ConvertPirAttribute2RuntimeAttribute not support
        # [pir::ArrayAttribute<pir::DoubleAttribute>]` from onednn_instruction.cc on
        # every OCR call. Setting the env var must happen before paddle is imported;
        # the explicit set_flags after import is belt-and-suspenders for cases where
        # paddle was already imported elsewhere.
        import os
        os.environ.setdefault("FLAGS_enable_pir_in_executor", "0")

        PaddleOCR = _try_import_paddleocr()
        import paddle  # type: ignore
        import paddleocr  # type: ignore
        paddle.set_flags({"FLAGS_enable_pir_in_executor": False})
        self.version = getattr(paddleocr, "__version__", "unknown")

        # Auto-detect GPU when not specified. Passing device="gpu" when no GPU is
        # actually present makes paddle silently fall back to CPU, and on that
        # fallback path the PIR-executor flag above is NOT honored — every OCR
        # call then fails with the ConvertPirAttribute error. Explicit device="cpu"
        # avoids that.
        if use_gpu is None:
            try:
                use_gpu = paddle.device.cuda.device_count() > 0
            except Exception:
                use_gpu = False

        self._impl = PaddleOCR(
            lang=lang,
            device="gpu" if use_gpu else "cpu",
            enable_mkldnn=False,
            # Magazine scans are already aligned; skip the doc-orientation and
            # UVDoc unwarping preprocessors so we don't run them on full-res input.
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            # PaddleOCR 3.x's default OCR pipeline ships `limit_side_len: 64,
            # limit_type: min`, which is effectively "never downscale". For a
            # 200-DPI magazine page (~3000 px) that asks PP-OCRv5_server_det for
            # ~33 GB. Cap detection input at the model's native 960-px training
            # resolution.
            text_det_limit_side_len=960,
            text_det_limit_type="max",
        )

    def recognize(self, image):
        import numpy as np  # type: ignore
        arr = np.array(image.convert("RGB"))
        results = self._impl.predict(arr)
        regions: list[OCRRegion] = []
        if not results:
            return regions
        result = results[0]
        texts = result.get("rec_texts", [])
        scores = result.get("rec_scores", [])
        polys = result.get("rec_polys", result.get("dt_polys", []))
        for text, confidence, poly in zip(texts, scores, polys):
            xs = [float(pt[0]) for pt in poly]
            ys = [float(pt[1]) for pt in poly]
            regions.append(OCRRegion(
                text=text,
                bbox=(min(xs), min(ys), max(xs), max(ys)),
                confidence=float(confidence),
            ))
        return regions
