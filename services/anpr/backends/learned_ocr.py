"""Two general-purpose learned OCR engines, selectable instead of Tesseract.

Chosen by measurement, 14 Sep 2026, on the 42 hand-verified government plates
(one crop each, zero-shot; stored exact, within 1, within 2):

    engine         exact   <=1   <=2   invented a plate on 25 unreadable crops
    Tesseract          5    13    20   2 (pipeline read)
    PaddleOCR-VL      27    41    41   3
    docTR PARSeq      24    30    31   1

Through the full pipeline on the 12 generated traffic clips (plates found
exactly; OCR time per crop):

    engine         night     day     CPU                 GPU (A5000)
    Tesseract      35/101   58/69    ~90-110 ms          n/a
    docTR PARSeq   63/101   66/69    37 ms, 4 threads    23 ms
    PaddleOCR-VL  101/101   69/69    12.5 s, 20 cores    ~440 ms

So docTR is the one that fits an edge box; PaddleOCR-VL needs a GPU.

Qwen3-VL-4B read more (31 exact) but wrote a plate for 19 of the 25 unreadable
crops, so it is not offered: an invented plate is the error an operator acts on.

Both need PyTorch, with transformers for PaddleOCR-VL and python-doctr for
docTR. The ANPR image carries docTR (CPU PyTorch); PaddleOCR-VL needs a GPU
image. `auto` (the default) uses docTR when it is installed, else Tesseract.
ANPR_OCR_BACKEND=paddleocr-vl, doctr or tesseract forces one. ANPR_OCR_DEVICE
picks cuda or cpu, defaulting to cuda if present.

Input handling matches the measurement: grey, enlarged to 96 px tall, RGB.
Confidence is the weakest character or token, as for the other backends.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any

from services.anpr.models import OcrResult

log = logging.getLogger("anpr.learned_ocr")

__all__ = ["DoctrPlateOcr", "PaddleVlPlateOcr", "prepare", "clean", "shared"]

INPUT_HEIGHT = 96
#: Longest read kept. Indian marks run to 10 characters (11 with a BH series
#: slip). Measured 14 Sep 2026 on a live government camera: PaddleOCR-VL
#: answered 86 of 431 unreadable crops with the sentence "The image is too
#: blurry to recognize any text content", and once with "The quick brown fox
#: jumps over the lazy dog". Cleaned to letters, those look like a plate
#: nobody could search for; they are refused instead.
MAX_PLATE_CHARS = 12
PADDLE_VL_MODEL = os.environ.get("ANPR_PADDLE_VL_MODEL", "PaddlePaddle/PaddleOCR-VL")
_NON_PLATE = re.compile(r"[^A-Z0-9]")


def clean(text: str | None) -> str:
    return _NON_PLATE.sub("", (text or "").upper())


def prepare(image: Any) -> Any:
    """BGR or grey crop -> grey, enlarged to 96 px tall (never shrunk), RGB."""
    import cv2

    grey = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = grey.shape[:2]
    if 0 < h < INPUT_HEIGHT:
        grey = cv2.resize(grey, (max(1, round(w * INPUT_HEIGHT / h)), INPUT_HEIGHT),
                          interpolation=cv2.INTER_CUBIC)
    return cv2.cvtColor(grey, cv2.COLOR_GRAY2RGB)


#: Torch threads per worker process on CPU. Measured at 4: 37 ms per crop.
CPU_THREADS = int(os.environ.get("ANPR_OCR_THREADS", "4"))


def _device() -> str:
    wanted = os.environ.get("ANPR_OCR_DEVICE")
    if wanted:
        return wanted
    import torch

    return "cuda" if torch.cuda.is_available() else "cpu"


def _cap_cpu_threads() -> None:
    import torch

    if _device() == "cpu":
        torch.set_num_threads(CPU_THREADS)


class _Lazy:
    """Loads the model on first read, once per process, and says so loudly."""

    name = "learned ocr"

    def __init__(self) -> None:
        self._model: Any = None
        self._error: str | None = None
        #: Per reader, not per module: a 440 ms PaddleOCR-VL read on a boosted
        #: camera must not hold up docTR on the others.
        self._lock = threading.Lock()

    def _load(self) -> Any:
        raise NotImplementedError

    def _infer(self, model: Any, rgb: Any) -> tuple[str, float]:
        raise NotImplementedError

    def ensure_loaded(self) -> str | None:
        """Load now if not yet loaded. Returns why it cannot load, or None."""
        with self._lock:
            return self._ensure_locked()

    def _ensure_locked(self) -> str | None:
        if self._model is None and self._error is None:
            try:
                self._model = self._load()
                log.info("OCR backend: %s on %s", self.name, _device())
            except Exception as exc:  # noqa: BLE001
                # Loud for the same reason as Tesseract: no reader looks like no traffic.
                log.exception("%s failed to load. NO PLATES WILL BE READ.", self.name)
                self._error = f"{type(exc).__name__}: {exc}"
        return self._error

    def read(self, image: Any) -> OcrResult | None:
        if image is None or image.size == 0:
            return None
        with self._lock:
            if self._ensure_locked():
                return None
            try:
                text, confidence = self._infer(self._model, prepare(image))
            except Exception:  # noqa: BLE001 - one bad crop must not stop the camera
                log.exception("OCR failed on one crop")
                return None
        text = clean(text)
        if not text or len(text) > MAX_PLATE_CHARS:
            return None
        return OcrResult(text=text, confidence=round(confidence, 4))


_shared: dict[type, _Lazy] = {}
_shared_lock = threading.Lock()


def shared(cls: type[_Lazy]) -> _Lazy:
    """One reader of this kind per process, however many cameras ask.

    Found 14 Sep 2026 with five government cameras on PaddleOCR-VL in one
    worker: each camera built its own reader, so five 0.9 B-parameter models
    loaded onto one GPU at once, and `transformers`' lazy import is not safe
    across threads — one camera failed with "cannot import name
    AutoModelForImageTextToText" and read nothing. One instance loads once,
    under its own lock, and every camera shares it.
    """
    with _shared_lock:
        if cls not in _shared:
            _shared[cls] = cls()
        return _shared[cls]


class DoctrPlateOcr(_Lazy):
    name = "doctr-parseq"

    def _load(self) -> Any:
        from doctr.models import recognition_predictor

        _cap_cpu_threads()
        return recognition_predictor("parseq", pretrained=True).to(_device()).eval()

    def _infer(self, model: Any, rgb: Any) -> tuple[str, float]:
        text, confidence = model([rgb])[0]
        return text, float(confidence)


class PaddleVlPlateOcr(_Lazy):
    name = "paddleocr-vl"

    def _load(self) -> Any:
        device = _device()
        if device != "cuda" and not os.environ.get("ANPR_PADDLE_VL_ALLOW_CPU"):
            # Measured 14 Sep 2026: 12.5 s per crop on 20 CPU cores. On CPU it
            # would hold an OCR slot for that long and starve every other camera
            # on the worker, so it refuses instead of running uselessly.
            raise RuntimeError("PaddleOCR-VL needs a GPU; this worker has none "
                               "(set ANPR_PADDLE_VL_ALLOW_CPU=1 to force it)")
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor

        _cap_cpu_threads()
        dtype = torch.bfloat16 if device == "cuda" else torch.float32
        # `.to(device)` rather than `device_map`, which would need `accelerate`.
        model = AutoModelForImageTextToText.from_pretrained(
            PADDLE_VL_MODEL, dtype=dtype).to(device).eval()
        return model, AutoProcessor.from_pretrained(PADDLE_VL_MODEL)

    def _infer(self, model: Any, rgb: Any) -> tuple[str, float]:
        import torch
        from PIL import Image

        net, proc = model
        messages = [{"role": "user", "content": [
            {"type": "image", "image": Image.fromarray(rgb)}, {"type": "text", "text": "OCR:"}]}]
        inputs = proc.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                          return_dict=True, return_tensors="pt").to(net.device)
        with torch.no_grad():
            out = net.generate(**inputs, max_new_tokens=24, do_sample=False,
                               output_scores=True, return_dict_in_generate=True)
        new = out.sequences[0, inputs["input_ids"].shape[1]:]
        text = proc.decode(new, skip_special_tokens=True)
        # Weakest token probability over the generated characters.
        probs = [torch.softmax(s[0].float(), -1)[t].item()
                 for s, t in zip(out.scores, new, strict=False)]
        special = set(proc.tokenizer.all_special_ids)
        kept = [p for p, t in zip(probs, new.tolist(), strict=False) if t not in special]
        return text, min(kept) if kept else 0.0
