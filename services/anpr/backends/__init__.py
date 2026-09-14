"""Stage factories: build a vehicle tracker, a plate locator or an OCR engine.

Each analytics stage has a light implementation and a heavy one, and this module
is the only place that knows which is which. That matters because the choice is
made in three different situations and they must agree:

  - the worker, choosing a default for the whole process;
  - the escalation policy, swapping a single camera up to the heavy path because
    the light one has been measured failing on it;
  - the accuracy harness, forcing one tier so the two can be compared.

The tiers, and what heavy costs, measured on this hardware:

    stage      light                    heavy                   cost ratio
    vehicles   motion proposals         YOLO (ONNX)             ~15x
    plate      classical locator        learned ONNX detector   ~60x
    OCR        docTR (else Tesseract)   learned hub recogniser  ~4x

The defaults are light, and that is a measurement rather than a preference; see
the notes on each tier below. But light by default is not the same as light
only. A camera the light path demonstrably cannot read is a camera where 60x of
nothing is worse value than 60x of something, which is what
services/anpr/escalation.py acts on.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from typing import Any

log = logging.getLogger("anpr.backends")

__all__ = [
    "LIGHT",
    "HEAVY",
    "require",
    "build_tracker",
    "build_locator",
    "build_ocr",
    "FallbackOcr",
    "resolve_tier",
]

#: Names for the two tiers, used in metrics, logs and the escalation record.
LIGHT = "light"
HEAVY = "heavy"


def resolve_tier(setting: str, *, light: tuple[str, ...], heavy: tuple[str, ...]) -> str:
    """Turn an env-var value into LIGHT or HEAVY. `auto` resolves to light."""
    lowered = (setting or "auto").lower()
    if lowered in heavy:
        return HEAVY
    if lowered in light:
        return LIGHT
    return LIGHT


# --- vehicles -----------------------------------------------------------

#: Which vehicle stage to use. `auto` runs the learned detector and falls back
#: to motion proposals on a camera where it returns nothing while the scene is
#: moving — which is a claim about the detector, not about the road.
VEHICLE_BACKEND = (os.environ.get("ANPR_VEHICLE_BACKEND") or "auto").lower()


def require(module: str, backend: str) -> None:
    """Fail at *build* time if a heavy backend's dependency is absent.

    The heavy backends import their libraries lazily, inside the inference call,
    so that this module stays importable on the lean image where those libraries
    are not installed. That is correct — but it means constructing one succeeds
    and the failure lands on the first *frame* instead.

    Escalation guards the construction and rolls a camera back to the light path
    when a model will not load. A failure deferred to first use walks straight
    past that guard: measured 31 Aug 2026, one camera escalating to YOLO on an
    image without `ultralytics` produced `frame failed` on every frame, for
    that camera *and* others, degrading an estate that had been working.

    Checking the spec here costs nothing and puts the failure where the handling
    already is.
    """
    import importlib.util

    if importlib.util.find_spec(module) is None:
        raise ModuleNotFoundError(
            f"the {backend} needs `{module}`, which is not installed in "
            f"this image. The lean image runs the light path by design; build "
            f"services/anpr/Dockerfile.gpu for the heavy models."
        )


def build_tracker(kind: str | None = None) -> Any:
    """Vehicle detector/tracker. `auto` composes learned-with-motion-fallback."""
    choice = (kind or VEHICLE_BACKEND).lower()
    from services.anpr.backends.motion import CompositeVehicleTracker, MotionVehicleTracker

    if choice in {"motion", LIGHT}:
        return MotionVehicleTracker()

    import importlib.util

    if importlib.util.find_spec("ultralytics") is None and choice not in {"yolo", HEAVY}:
        # `auto` means "the best available", not "the heaviest named". Building
        # a composite around a primary that cannot import gives a tracker that
        # raises on every frame — and the composite falls back on *empty
        # results*, not on exceptions, so nothing catches it.
        #
        # Measured 31 Aug 2026: the compose file sets ANPR_VEHICLE_BACKEND to an
        # empty string, `os.environ.get(k, "auto")` returned that empty string
        # rather than the default, and `auto` therefore selected the heaviest
        # path on an image that has none of it. The whole estate went to zero
        # sightings, reporting `frame failed` per camera per frame.
        log.info("ultralytics is not installed; `%s` resolves to motion", choice)
        return MotionVehicleTracker()

    from services.anpr.backends.yolo import YoloVehicleTracker

    if choice in {"yolo", HEAVY}:
        # Only the *explicit* heavy choice is required to be satisfiable.
        #
        # The composite below is a different case and must not be guarded: it
        # pairs YOLO with a motion fallback precisely so that a missing or
        # broken learned model degrades to the light path instead of stopping
        # the camera. Requiring the dependency here too would turn that designed
        # graceful path into a hard startup failure — measured 31 Aug 2026, when
        # an over-broad check took the whole estate to zero sightings.
        require("ultralytics", "yolo vehicle tracker")
        return YoloVehicleTracker()
    return CompositeVehicleTracker(YoloVehicleTracker(), MotionVehicleTracker())


# --- plate location -----------------------------------------------------

#: `learned` is the ONNX plate detector, `classical` the adaptive-threshold
#: locator that needs no model.
#:
#: `auto` resolves to **classical**, and this is a measurement. Probed over the
#: same clip, same vehicle tracker, same OCR:
#:
#:     locator     vehicles read   plate_detect p50
#:     learned          13 / 14           55.45 ms
#:     classical        13 / 14            0.93 ms
#:
#: Identical read rate, 60x the cost. On a CPU-bound estate that difference is
#: the whole budget — `plate_detect` was the second most expensive stage under
#: load, and it becomes free. Indian plates are dark-on-white with a strong
#: aspect ratio, which is exactly what the classical locator is good at.
#:
#: That holds *on these clips*. A learned detector should win on cluttered
#: real-world scenes, and on a GPU its cost would be negligible — which is
#: exactly why it stays reachable, both by env var and, per camera, by
#: escalation when the classical locator is measured failing.
PLATE_BACKEND = (os.environ.get("ANPR_PLATE_BACKEND") or "auto").lower()


def build_locator(kind: str | None = None) -> Any:
    choice = (kind or PLATE_BACKEND).lower()
    from services.anpr.backends.plates import ClassicalPlateLocator, PlateLocatorBackend

    if choice in {"learned", "hub", HEAVY}:
        require("onnxruntime", "learned plate locator")
        return PlateLocatorBackend()
    return ClassicalPlateLocator()


# --- OCR ----------------------------------------------------------------

#: `hub` is the learned plate-recognition model; `tesseract` is the lean path
#: that needs no download and runs on an edge box.
#:
#: *Superseded 14 Sep 2026:* `auto` now prefers docTR where installed (see
#: `build_ocr` and `learned_ocr.py`). The note below is why the hub model lost,
#: and still holds.
#:
#: `auto` preferred **Tesseract**, and that was a measurement rather than a
#: preference. Scored against the generated clips' ground truth on 738
#: sightings, the hub model `cct-s-v1-global-model` returned **1.1% exact**
#: (16.7% within edit distance 2); the same pipeline on Tesseract read 12 of 14
#: tracked vehicles with 83% exact. The hub models are trained on European and
#: Latin American plates and systematically misread the Indian font — `GJ` comes
#: back as `2J`, `3J`, `6J` — and positional normalisation cannot repair a first
#: character that was never a letter to begin with.
#:
#: `hub` stays available because that conclusion is about the models that exist
#: today, and because a camera where Tesseract reads nothing at all has nothing
#: to lose by trying something else.
OCR_BACKEND = (os.environ.get("ANPR_OCR_BACKEND") or "auto").lower()


def build_ocr(kind: str | None = None) -> Any:
    choice = (kind or OCR_BACKEND).lower()
    from services.anpr.backends.tesseract import TesseractPlateOcr

    if choice in {"hub", HEAVY}:
        require("onnxruntime", "hub plate recogniser")
        from services.anpr.backends.plates import PlateOcrBackend

        return PlateOcrBackend()

    # General learned OCR, measured far ahead of Tesseract on the government
    # plates (see learned_ocr.py). Opt-in: they need PyTorch, which the lean
    # image does not carry.
    if choice == "paddleocr-vl":
        require("transformers", "PaddleOCR-VL recogniser")
        from services.anpr.backends.learned_ocr import PaddleVlPlateOcr, shared

        return shared(PaddleVlPlateOcr)

    if choice == "doctr":
        require("doctr", "docTR recogniser")
        from services.anpr.backends.learned_ocr import DoctrPlateOcr, shared

        return shared(DoctrPlateOcr)

    if choice == "hub-first":
        require("onnxruntime", "hub plate recogniser")
        from services.anpr.backends.plates import PlateOcrBackend

        return FallbackOcr(PlateOcrBackend(), TesseractPlateOcr())

    # `auto`: docTR where it is installed, Tesseract where it is not. Decided
    # 14 Sep 2026 on measurement: docTR found 63 of 101 night plates against
    # Tesseract's 35, read 24 of 42 real government plates exactly against 5,
    # and took 37 ms per crop on 4 CPU threads against Tesseract's ~100 ms.
    # Checked by import spec, not by trying a read: a first crop with nothing on
    # it must not decide the engine for the life of the process.
    if choice == "auto" and importlib.util.find_spec("doctr") is not None:
        from services.anpr.backends.learned_ocr import DoctrPlateOcr, shared

        return shared(DoctrPlateOcr)

    return TesseractPlateOcr()


class FallbackOcr:
    """Learned model where it loaded, Tesseract where it did not.

    Decided once, on the first crop, rather than per read: two OCR engines
    alternating on one camera would produce two different error distributions
    voting into the same plate.
    """

    def __init__(self, primary: Any, secondary: Any) -> None:
        self.primary = primary
        self.secondary = secondary
        self.chosen: Any = None

    def read(self, image: Any) -> Any:
        if self.chosen is None:
            result = self.primary.read(image)
            if result is not None:
                self.chosen = self.primary
                return result
            self.chosen = self.secondary
        return self.chosen.read(image)
