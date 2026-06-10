"""EasyOCR wrapper. Heavyweight model — loaded lazily, kept as a process singleton.

Tests should monkeypatch `_reader` or pass a custom `read_image_bytes` impl rather
than installing easyocr.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    import easyocr  # noqa: F401

log = logging.getLogger(__name__)

_reader = None  # populated on first call

CONF_THRESHOLD = 0.5


def _gpu_available() -> bool:
    """Detect whether PyTorch can see a CUDA GPU.

    `EASYOCR_FORCE_CPU=1` in the environment overrides to CPU — useful when
    you want to debug something on a machine that has a GPU but the GPU is
    being used by something else.
    """
    if os.environ.get("EASYOCR_FORCE_CPU") == "1":
        return False
    try:
        import torch  # noqa: PLC0415  — lazy so non-OCR code paths don't pay
        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        return False


@dataclass
class OcrResult:
    text: str               # joined tokens above threshold
    needs_review: bool      # True if any token was below threshold
    raw_tokens: list[tuple[str, float]]  # all tokens for debugging


def _get_reader():
    global _reader
    if _reader is None:
        import easyocr  # local import — heavy dep
        use_gpu = _gpu_available()
        log.info("easyocr: loading model (gpu=%s)", use_gpu)
        _reader = easyocr.Reader(["en"], gpu=use_gpu)
    return _reader


def read_image_bytes(image_bytes: bytes) -> OcrResult:
    """Run OCR over raw image bytes. Returns text and review flag."""
    import numpy as np
    from PIL import Image
    import io

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    arr = np.array(img)
    raw = _get_reader().readtext(arr)  # list of (bbox, text, conf)
    tokens = [(t, float(c)) for _, t, c in raw]
    above = [t for t, c in tokens if c >= CONF_THRESHOLD]
    needs_review = any(c < CONF_THRESHOLD for _, c in tokens)
    return OcrResult(text=" ".join(above), needs_review=needs_review, raw_tokens=tokens)


def from_raw_tokens(tokens: list[tuple[str, float]]) -> OcrResult:
    """Build an OcrResult from already-extracted tokens. Used by tests and by
    pipeline code that wants to inject pre-OCR'd content."""
    above = [t for t, c in tokens if c >= CONF_THRESHOLD]
    needs_review = any(c < CONF_THRESHOLD for _, c in tokens)
    return OcrResult(text=" ".join(above), needs_review=needs_review, raw_tokens=list(tokens))


def reset_reader() -> None:
    """Test hook: drop the cached reader."""
    global _reader
    _reader = None
