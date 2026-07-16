import contextlib
import io
import logging
import sys
import warnings
from dataclasses import dataclass
from typing import Protocol

from PIL import Image


@dataclass(frozen=True)
class OCRRegion:
    text: str
    bbox: tuple[float, float, float, float]  # x0, y0, x1, y1
    confidence: float


class FatalOCRError(RuntimeError):
    """An OCR failure that has corrupted process-wide state and cannot be
    recovered from in-process.

    The canonical case is a CUDA illegal-memory-access (cudaErrorIllegalAddress,
    error 700): once the GPU context is in this state every *subsequent* CUDA
    call returns the same error, so continuing to OCR just yields empty text for
    the rest of the run. CUDA's own guidance is that the process must be
    terminated and relaunched. Unlike an ordinary per-page OCR failure this must
    NOT be swallowed — callers should abort and let the operator restart the
    process, otherwise a whole batch of bundles is silently produced with no
    text and no way to tell them apart from good ones.
    """


# Substrings that mark a GPU failure as sticky/unrecoverable: the CUDA context
# is corrupt and every later call raises the same thing. Matched case-
# insensitively against the exception text. Deliberately excludes plain
# out-of-memory ("Out of memory" / cudaErrorMemoryAllocation), which is often
# per-image recoverable — a smaller following page can still succeed.
_UNRECOVERABLE_GPU_SIGNATURES = (
    "must be terminated and relaunched",
    "illegal memory access",
    "cudaerrorillegaladdress",
    "misaligned address",
    "cudaerrorlaunchfailure",
    "an illegal instruction was encountered",
)


def _is_unrecoverable_gpu_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(sig in text for sig in _UNRECOVERABLE_GPU_SIGNATURES)


class OCREngine(Protocol):
    name: str
    version: str

    def recognize(self, image: Image.Image) -> list[OCRRegion]: ...


class FakeOCREngine:
    """Deterministic test double. Returns scripted responses then falls back to default."""

    name: str = "fake"
    version: str = "0.0.1"

    def __init__(
        self, responses: list[list[OCRRegion] | BaseException] | None = None
    ) -> None:
        self._responses = list(responses or [])
        self._index = 0
        self._default = [OCRRegion(text="fake page", bbox=(0, 0, 100, 20), confidence=1.0)]

    def recognize(self, image: Image.Image) -> list[OCRRegion]:
        if self._index < len(self._responses):
            r = self._responses[self._index]
            self._index += 1
            # A scripted exception is raised, letting tests exercise the
            # failure paths (e.g. a fatal GPU crash mid-run).
            if isinstance(r, BaseException):
                raise r
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


@contextlib.contextmanager
def _silence_paddle_chatter(enabled: bool):
    """Suppress paddle/paddlex init noise:
    * the ccache `UserWarning` paddle emits when first imported,
    * the `Creating model: ...` and `Model files already exist...` lines
      paddlex logs while PaddleOCR loads its detection / recognition /
      orientation models.

    The latter go through paddlex's own logger (`paddlex.utils.logging`),
    which writes directly to stderr with `propagate=False`, so neither a
    stdout redirect nor a root-logger setting suppresses them. paddlex
    also calls `setup_logging()` at import time, resetting its level to
    INFO — so we have to import paddlex first, then lower the level."""
    if not enabled:
        yield
        return
    noisy_loggers = ("paddle", "paddlex", "paddleocr", "ppocr")
    prior_levels = {n: logging.getLogger(n).level for n in noisy_loggers}
    try:
        with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
            warnings.filterwarnings("ignore", category=UserWarning)
            try:
                import paddlex  # noqa: F401  — triggers setup_logging() at INFO
            except Exception:
                pass
            for n in noisy_loggers:
                logging.getLogger(n).setLevel(logging.WARNING)
            yield
    finally:
        for n, level in prior_levels.items():
            logging.getLogger(n).setLevel(level)


def _check_paddle_compat(paddle) -> None:
    """Catch paddle ↔ paddleocr/paddlex version skew before paddleocr does, with
    a clearer message. paddlex 3.x calls AnalysisConfig.set_optimization_level
    unconditionally; that method was added in paddlepaddle 3.x. PyPI's
    `paddlepaddle-gpu` is pinned to 2.x, so users who `pip install paddlepaddle-gpu`
    without using PaddlePaddle's own index land here."""
    try:
        AnalysisConfig = paddle.base.libpaddle.AnalysisConfig  # noqa: N806
    except AttributeError:
        return  # different paddle layout — let paddleocr surface its own error
    if hasattr(AnalysisConfig, "set_optimization_level"):
        return
    version = getattr(paddle, "__version__", "unknown")
    raise RuntimeError(
        f"installed paddlepaddle ({version}) is too old for paddleocr 3.x — "
        "AnalysisConfig.set_optimization_level is missing. "
        "Install paddlepaddle (CPU) or paddlepaddle-gpu >= 3.0 from PaddlePaddle's "
        "own index, e.g.:\n"
        "  pip install paddlepaddle-gpu==3.3.1 "
        "--extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cu118/\n"
        "(replace cu118 with your CUDA version)."
    )


def _probe_gpu(paddle) -> tuple[bool, str]:
    """Decide whether to enable GPU and explain why. Returns (use_gpu, reason)."""
    if not paddle.is_compiled_with_cuda():
        return False, (
            "installed paddlepaddle is CPU-only — install paddlepaddle-gpu for GPU"
        )
    try:
        count = paddle.device.cuda.device_count()
    except Exception as exc:  # pragma: no cover
        return False, f"CUDA probe failed: {exc}"
    if count == 0:
        return False, "paddle reports 0 CUDA devices"
    return True, f"detected {count} CUDA device(s)"


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

    def __init__(
        self,
        lang: str = "en",
        use_gpu: bool | None = None,
        verbose: bool = False,
    ) -> None:
        # Disable PIR-based execution. Under PaddlePaddle 3.3.x on CPU the PIR executor
        # raises `ConvertPirAttribute2RuntimeAttribute not support
        # [pir::ArrayAttribute<pir::DoubleAttribute>]` from onednn_instruction.cc on
        # every OCR call. Setting the env var must happen before paddle is imported;
        # the explicit set_flags after import is belt-and-suspenders for cases where
        # paddle was already imported elsewhere.
        import os
        os.environ.setdefault("FLAGS_enable_pir_in_executor", "0")

        # Set once a fatal GPU error has poisoned the context; every later
        # recognize() then fails fast instead of re-triggering the same crash.
        self._dead = False

        with _silence_paddle_chatter(enabled=not verbose):
            PaddleOCR = _try_import_paddleocr()
            import paddle  # type: ignore
            import paddleocr  # type: ignore
            paddle.set_flags({"FLAGS_enable_pir_in_executor": False})
            self.version = getattr(paddleocr, "__version__", "unknown")
            _check_paddle_compat(paddle)

            # Auto-detect GPU when not specified. Passing device="gpu" when no GPU is
            # actually present makes paddle silently fall back to CPU, and on that
            # fallback path the PIR-executor flag above is NOT honored — every OCR
            # call then fails with the ConvertPirAttribute error. Explicit device="cpu"
            # avoids that.
            if use_gpu is None:
                use_gpu, reason = _probe_gpu(paddle)
            elif use_gpu and not paddle.is_compiled_with_cuda():
                raise RuntimeError(
                    "GPU requested but installed paddlepaddle is CPU-only. "
                    "Install paddlepaddle-gpu (matching your CUDA version) to use a GPU."
                )
            else:
                reason = "explicitly requested"
            print(
                f"[ocr] device: {'gpu' if use_gpu else 'cpu'} ({reason})",
                file=sys.stderr,
            )

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
        if self._dead:
            raise FatalOCRError(
                "OCR engine is unusable after a fatal GPU error earlier in this "
                "run — the process must be restarted before OCR can continue"
            )
        arr = np.array(image.convert("RGB"))
        try:
            results = self._impl.predict(arr)
        except Exception as exc:
            # A sticky CUDA fault (illegal address, launch failure, …) corrupts
            # the context for the rest of the process: mark the engine dead and
            # surface a distinct error so callers abort instead of silently
            # OCR-ing every remaining page to empty text.
            if _is_unrecoverable_gpu_error(exc):
                self._dead = True
                raise FatalOCRError(f"unrecoverable GPU error during OCR: {exc}") from exc
            raise
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
