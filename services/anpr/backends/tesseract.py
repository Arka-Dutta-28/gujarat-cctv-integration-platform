"""Plate OCR with Tesseract.

The lean path: no neural network, no GPU, no model download. It matters for
three reasons, and only one of them is convenience.

It makes the edge claim concrete. The architecture says analytics run next to
the camera and only metadata crosses the network. That argument is much stronger
when the analytics stack is background subtraction, a contour filter and a 15 MB
OCR engine, all of which run on the kind of box that can actually be mounted in
a junction cabinet.

It is the fallback that keeps the demo alive. A learned OCR model is a download
away from working, and a download is a way to fail in front of evaluators.

Indian plates suit it. A single-row mark in a mono-ish face, dark on white or
yellow, is close to Tesseract's best case once the crop is rectified and
binarised.

The honest limitation: on a small, blurred or heavily glared plate it is clearly
worse than a trained plate-recognition model, and it hallucinates punctuation.
The character whitelist and the positional normalisation in
services/common/plates.py absorb most of that, and the per-track vote absorbs
the rest, but a run measured with this backend is reported as such.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from services.anpr.models import OcrResult

log = logging.getLogger("anpr.tesseract")

__all__ = ["TesseractPlateOcr", "preprocess", "assemble"]

#: Only the characters an Indian registration mark can contain. Removing the
#: rest is the single largest accuracy improvement available here — most of
#: Tesseract's mistakes on a plate are punctuation it invented from the border.
WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

#: PSM 7: treat the crop as a single line of text. The crop *is* a single line
#: of text, and telling Tesseract so stops it looking for paragraph structure.
CONFIG = f"--oem 1 --psm 7 -c tessedit_char_whitelist={WHITELIST}"

#: Tesseract reads small text badly. Plate crops off a 1080p frame are often
#: 90 px wide, so they are enlarged before reading.
TARGET_HEIGHT = int(os.environ.get("ANPR_OCR_TARGET_HEIGHT", "64"))

_NON_PLATE = re.compile(r"[^A-Z0-9]")


def preprocess(image: Any) -> Any:
    """Greyscale, enlarge, and binarise a plate crop for OCR.

    Otsu rather than a fixed threshold because the same camera sees the same
    plate at wildly different exposures between day, night and headlight glare,
    and a fixed threshold tuned on one of those fails on the others.
    """
    import cv2

    grey = image if len(image.shape) == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = grey.shape[:2]
    if height == 0 or width == 0:
        return grey

    if height < TARGET_HEIGHT:
        scale = TARGET_HEIGHT / height
        grey = cv2.resize(
            grey, (max(1, int(width * scale)), TARGET_HEIGHT), interpolation=cv2.INTER_CUBIC
        )

    grey = cv2.bilateralFilter(grey, 5, 50, 50)
    _, binary = cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Indian plates are dark characters on a light ground. If the crop came out
    # mostly dark the polarity is inverted — a night frame, or a black
    # commercial plate — and Tesseract wants dark-on-light either way.
    if binary.mean() < 127:
        binary = cv2.bitwise_not(binary)
    return binary


def assemble(data: dict) -> OcrResult | None:
    """Turn Tesseract's per-fragment output into one plate read.

    Separated from the image handling so the part that decides what a read is
    *worth* can be tested without OpenCV — that number feeds the per-track vote
    and from there every confidence figure in the submission.
    """
    pieces: list[str] = []
    confidences: list[float] = []
    for text, conf in zip(data.get("text", []), data.get("conf", []), strict=False):
        cleaned = _NON_PLATE.sub("", (text or "").upper())
        try:
            confidence = float(conf)
        except (TypeError, ValueError):
            continue
        if cleaned and confidence >= 0:
            pieces.append(cleaned)
            confidences.append(confidence / 100.0)

    joined = "".join(pieces)
    if not joined:
        return None

    # The weakest fragment, not the mean: a plate is only as readable as its
    # least certain part, and averaging hides one unreadable group behind
    # several clear ones — which is exactly the read the vote must weight down.
    return OcrResult(text=joined, confidence=round(min(confidences), 4))


class TesseractPlateOcr:
    """Reads a plate crop. Returns None rather than guessing at nothing."""

    def __init__(self, config: str = CONFIG) -> None:
        self.config = config
        self._available: bool | None = None

    def read(self, image: Any) -> OcrResult | None:
        if not self._ready():
            return None

        import pytesseract

        try:
            prepared = preprocess(image)
            data = pytesseract.image_to_data(
                prepared, config=self.config, output_type=pytesseract.Output.DICT
            )
        except Exception:  # noqa: BLE001 - one bad crop must not stop the camera
            log.exception("OCR failed on one crop")
            return None

        return assemble(data)

    def _ready(self) -> bool:
        if self._available is None:
            try:
                import pytesseract

                pytesseract.get_tesseract_version()
                self._available = True
                log.info("OCR backend: tesseract")
            except Exception as exc:  # noqa: BLE001
                # Loud: an absent OCR backend is indistinguishable from a camera
                # that sees no traffic, and would corrupt every accuracy figure.
                log.error(
                    "tesseract unavailable (%s). NO PLATES WILL BE READ — "
                    "do not report accuracy figures from this run.", exc,
                )
                self._available = False
        return self._available
