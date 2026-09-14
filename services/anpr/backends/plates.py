"""Plate localisation and character reading.

Two stages, both operating strictly inside a vehicle box the pipeline has
already chosen. Neither ever sees a whole frame, which is what keeps burnt-in
overlay text out of sightings.

Localisation. A learned detector (open-image-models, ONNX) where it is
available, with a classical fallback that finds bright, wide, high-contrast
rectangles. The fallback exists because a demo that cannot run without a model
download is a demo with an extra way to fail on the day, and because on the
Indian plate, dark characters on a white or yellow ground with a strong aspect
ratio, the classical route is genuinely competitive.

Reading. fast-plate-ocr where available. Its models are trained largely on
European and Latin American plates, so its raw output on an Indian mark is
imperfect, which is exactly what the positional normalisation in
services/common/plates.py exists to repair and why it coerces by slot rather
than globally. Whatever it returns is passed through normalise_plate by the
voting stage, and a read that will not resolve into the Indian grammar is kept
and flagged rather than dropped.

Both stages report honestly when they are unavailable. A backend that silently
returns nothing looks identical to a camera that sees no traffic, and that would
quietly corrupt every accuracy figure in the submission.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from services.anpr.models import Box, OcrResult, PlateDetection

log = logging.getLogger("anpr.plates")

__all__ = [
    "PlateLocatorBackend",
    "PlateOcrBackend",
    "ClassicalPlateLocator",
    "prewarm",
    "prewarm_detector",
    "prewarm_reader",
]

PLATE_MODEL = os.environ.get("ANPR_PLATE_MODEL", "yolo-v9-t-384-license-plate-end2end")
OCR_MODEL = os.environ.get("ANPR_OCR_MODEL", "cct-s-v1-global-model")

#: Indian plates are roughly 2:1 (single row) or 1:1 (two row). Anything much
#: outside that is a bumper edge, a light or a sticker.
MIN_ASPECT = 1.6
MAX_ASPECT = 6.0
MIN_PLATE_AREA_PX = 200

#: Threads per ONNX session. Left to itself, ONNX Runtime sizes its thread pool
#: from the core count, so *every* concurrent inference asks for one thread per
#: core. The parallelism here is already one decode thread per camera, so on a
#: 20-core box carrying 81 cameras that request compounded into roughly 1,600
#: threads and the machine spent its time context-switching rather than
#: inferring: `plate_detect` p50 reached 57 s with all six workers pinned at
#: 100% CPU. One thread per session, parallel across cameras, is the shape that
#: actually fits the work.
ORT_THREADS = int(os.environ.get("ANPR_ORT_THREADS", "1"))

#: Execution providers in order of precedence. Worth naming explicitly: the
#: default is *every* available provider, which on this image puts
#: `AzureExecutionProvider` — remote inference — ahead of the local CPU.
ORT_PROVIDERS = ("CUDAExecutionProvider", "CPUExecutionProvider")

_lock = threading.Lock()
_detector: Any = None
_reader: Any = None
_detector_tried = False
_reader_tried = False


class ClassicalPlateLocator:
    """Finds plate-shaped bright rectangles inside a vehicle crop.

    No model, no download, no GPU. Adaptive threshold, then contours filtered by
    aspect ratio and area. Weaker than a learned detector on a cluttered scene,
    but on a plate that is legible at all it usually finds the right rectangle,
    and it costs microseconds.
    """

    def locate(self, image: Any) -> list[PlateDetection]:
        import cv2

        if image is None or getattr(image, "size", 0) == 0:
            return []
        height, width = image.shape[:2]
        if height < 8 or width < 8:
            return []

        grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        # Plates are locally brighter than the bodywork around them, whatever
        # the overall exposure — which is what makes this survive night frames.
        binary = cv2.adaptiveThreshold(
            cv2.bilateralFilter(grey, 5, 40, 40), 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 25, -8,
        )
        closed = cv2.morphologyEx(
            binary, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (17, 5))
        )
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        found: list[PlateDetection] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if h == 0 or w * h < MIN_PLATE_AREA_PX:
                continue
            aspect = w / h
            if not MIN_ASPECT <= aspect <= MAX_ASPECT:
                continue
            if w > width * 0.95 or h > height * 0.6:
                continue
            # A plate sits low on a vehicle. Ranking by that rather than
            # filtering on it, so an unusual mounting is deprioritised, not lost.
            lowness = (y + h / 2) / height
            fill = cv2.contourArea(contour) / float(w * h)
            found.append(
                PlateDetection(
                    box=Box(x, y, x + w, y + h),
                    confidence=round(min(0.35 + 0.35 * lowness + 0.2 * fill, 0.9), 3),
                )
            )

        found.sort(key=lambda p: -p.confidence)
        return found[:3]


class PlateLocatorBackend:
    """Learned detector where available, classical otherwise."""

    def __init__(self) -> None:
        self.fallback = ClassicalPlateLocator()

    def locate(self, image: Any) -> list[PlateDetection]:
        detector = _shared_detector()
        if detector is None:
            return self.fallback.locate(image)
        try:
            results = detector.predict(image)
        except Exception:  # noqa: BLE001 - a bad frame must not stop the camera
            log.exception("plate detector failed; falling back for this frame")
            return self.fallback.locate(image)

        return [
            PlateDetection(
                box=Box(int(r.bounding_box.x1), int(r.bounding_box.y1),
                        int(r.bounding_box.x2), int(r.bounding_box.y2)),
                confidence=float(r.confidence),
            )
            for r in results
        ]


class PlateOcrBackend:
    """Reads characters off a plate crop."""

    def read(self, image: Any) -> OcrResult | None:
        reader = _shared_reader()
        if reader is None:
            return None
        try:
            result = reader.run(image, return_confidence=True)
        except Exception:  # noqa: BLE001
            log.exception("OCR failed on one crop")
            return None

        text, confidence = _unpack(result)
        if not text:
            return None
        return OcrResult(text=text, confidence=confidence)


def _unpack(result: Any) -> tuple[str, float]:
    """Normalise fast-plate-ocr's return shape into (text, confidence).

    Three shapes have to be handled, because getting this wrong is silent and
    expensive. Current versions return `list[PlatePrediction]`, where the
    prediction carries `.plate` and `.char_probs`. Older ones returned a bare
    string or a `(texts, confidences)` tuple. Reading the new shape with the old
    code does not raise — it stringifies the dataclass — so 25,157 sightings
    were written here with `PlatePrediction(plate='2J2ZN4469', char_probs=...)`
    as the plate before anyone noticed. Anything unrecognised is refused rather
    than coerced: a plate we cannot read is a fact, and inventing one from a
    repr is worse than reading nothing.

    Confidence is the *weakest* character rather than the mean. A plate is only
    as readable as its least certain slot, and averaging would hide a single
    unreadable character behind seven clear ones — precisely the read the vote
    needs to weight down.
    """
    if isinstance(result, (list, tuple)) and len(result) == 2 and _is_text(result[0]):
        texts, confidences = result
        return _first_text(texts), _weakest(_first(confidences))

    first = result[0] if isinstance(result, (list, tuple)) and result else result

    plate = getattr(first, "plate", None)
    if plate is not None:
        return str(plate).strip(), _weakest(getattr(first, "char_probs", None))

    if isinstance(first, str):
        return first.strip(), 0.5

    log.warning("unrecognised OCR return shape %s; read discarded", type(first).__name__)
    return "", 0.0


def _is_text(value: Any) -> bool:
    """Whether this looks like the legacy `texts` half of a 2-tuple."""
    if isinstance(value, str):
        return True
    return isinstance(value, (list, tuple)) and bool(value) and isinstance(value[0], str)


def _first(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and value:
        return value[0]
    return value


def _first_text(texts: Any) -> str:
    if isinstance(texts, str):
        return texts.strip()
    if isinstance(texts, (list, tuple)) and texts:
        return str(texts[0]).strip()
    return ""


def _weakest(probs: Any) -> float:
    """The least certain character's probability, clamped into [0, 1]."""
    if probs is None:
        return 0.5
    try:
        values = [float(v) for v in probs]
    except (TypeError, ValueError):
        return 0.5
    if not values:
        return 0.5
    return round(min(max(min(values), 0.0), 1.0), 4)


def _ort_config() -> tuple[list[str], Any]:
    """Session options and providers shared by both ONNX stages.

    Returns an empty configuration if ONNX Runtime is not importable, so the
    callers below still reach their own fallbacks rather than dying here.
    """
    try:
        import onnxruntime as ort
    except ImportError:
        return [], None

    available = set(ort.get_available_providers())
    providers = [p for p in ORT_PROVIDERS if p in available]

    options = ort.SessionOptions()
    options.intra_op_num_threads = ORT_THREADS
    options.inter_op_num_threads = ORT_THREADS
    return providers, options


def prewarm_detector() -> bool:
    """Resolve the learned plate detector now. See `prewarm` for why."""
    return _shared_detector() is not None


def prewarm_reader() -> bool:
    """Resolve the learned plate reader now. See `prewarm` for why."""
    return _shared_reader() is not None


def prewarm() -> tuple[bool, bool]:
    """Resolve both learned handles now, before any camera thread needs one.

    Both loaders fetch their ONNX weights on first use and hold `_lock` for the
    whole download. Reached lazily from the first crop, that download blocks
    *every* camera thread in the process, not just the one that triggered it:
    measured here, `plate_detect` p50 hit 121 s and mean decode across the
    estate fell to 0.07 fps while six replicas each pulled the same 7.4 MB over
    a link that manages ~50 kB/s. The models are not slow — the pipeline was
    simply standing in a queue behind a socket.

    Resolving them at startup converts a silent mid-flight stall into a startup
    cost that is visible in the log and charged to nothing.

    Returns whether the detector and the reader each loaded, so the caller can
    say plainly which path this run is actually on.
    """
    return _shared_detector() is not None, _shared_reader() is not None


def _shared_detector() -> Any:
    global _detector, _detector_tried
    with _lock:
        if not _detector_tried:
            _detector_tried = True
            try:
                from open_image_models import LicensePlateDetector

                providers, options = _ort_config()
                _detector = LicensePlateDetector(
                    detection_model=PLATE_MODEL,
                    providers=providers or None,
                    sess_options=options,
                )
                log.info("plate detector: %s on %s", PLATE_MODEL, providers or "default")
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "plate detector unavailable (%s); using the classical locator. "
                    "This is a real accuracy difference and belongs in the report.",
                    exc,
                )
                _detector = None
        return _detector


def _shared_reader() -> Any:
    global _reader, _reader_tried
    with _lock:
        if not _reader_tried:
            _reader_tried = True
            try:
                from fast_plate_ocr import LicensePlateRecognizer

                providers, options = _ort_config()
                _reader = LicensePlateRecognizer(
                    hub_ocr_model=OCR_MODEL,
                    providers=providers or None,
                    sess_options=options,
                )
                log.info("plate OCR: %s on %s", OCR_MODEL, providers or "default")
            except Exception as exc:  # noqa: BLE001
                # Loud, and repeated in the metrics: an OCR backend that is
                # quietly absent is indistinguishable from a camera that sees
                # no traffic, and would corrupt every accuracy figure.
                log.error(
                    "OCR model unavailable (%s). NO PLATES WILL BE READ — "
                    "do not report accuracy figures from this run.", exc,
                )
                _reader = None
        return _reader
