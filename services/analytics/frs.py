"""Face detection as a second analytics module, shipped disabled.

Read this before enabling anything here.

The technical proposal asks for an analytics approach including "technologies
such as ANPR, Facial Recognition Systems (FRS), object detection, person and
vehicle tracking". "Such as" makes the list illustrative, and the brief
separately confirms ANPR is the mandatory capability. So the platform is obliged
to address this class of analytics. It is not obliged to run it against public
CCTV, and it does not.

What this module is for. One thing, and it is architectural rather than
operational: it demonstrates that the edge pipeline takes analytics modules as
plugins rather than having ANPR welded into it. That is a load-bearing claim in
docs/hld.md, and a claim with exactly one implementation behind it is an
assertion. A second working module is the evidence.

What it deliberately is not. It does not identify anyone. There is no gallery,
no enrolment, no matching against a watchlist of persons, and no API that
returns a name. It reports that a face-shaped region exists, with a bounding box
and a confidence, which is the same shape of output the vehicle detector
produces. That is the point.

Adding identification is not a small step and must not look like one. It needs a
lawful authorisation regime, per-query case binding, an audit entry per query,
and a retention policy for the biometric templates themselves. Those are named
in docs/hld.md section 5.6 as conditions, not as future work someone can quietly
tick off.

Why it is disabled by default. Deploying facial recognition against public CCTV
raises live questions under the DPDP Act 2023, and India has no settled
regulatory framework for it. A system that can be switched on under
authorisation, and that records why it was, is defensible. One that is silently
on by default is not, and "we shipped it disabled" is only true if the default
is actually off, which is why FRS_ENABLED has no truthy default anywhere in this
repository.

Demonstration policy. Synthetic faces only.
scripts/make_synthetic_faces.py draws them; they belong to no person, so no
consent question arises and no real individual's biometrics are processed. This
module is never pointed at a government feed or at a recording of a real person,
and the demo material carries none.
"""

from __future__ import annotations

import logging
import os
import pathlib
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("analytics.frs")

__all__ = ["FaceDetection", "FaceAnalytic", "CASCADE_CANDIDATES", "frs_enabled"]

#: Where Debian and the common OpenCV wheels keep the frontal-face cascade.
#: Searched in order; the first that exists wins. A list rather than one path
#: because the module must degrade to "unavailable" on an image that does not
#: ship the data, not crash the worker that hosts it.
CASCADE_CANDIDATES: tuple[str, ...] = (
    "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
    "/usr/share/opencv/haarcascades/haarcascade_frontalface_default.xml",
)

#: Below this many neighbours a detection is noise. Deliberately conservative:
#: a false positive here is a face-shaped region reported where there is no
#: face, and this module's whole justification is restraint.
MIN_NEIGHBOURS = 6

#: As a fraction of frame height, not pixels. The grid is not uniform and no
#: threshold in this platform may be in absolute pixels — a constant tuned for
#: 1080p silently ignores people on a small camera and wastes work on a large
#: one, at the same time.
MIN_FACE_FRACTION = 0.06


def frs_enabled() -> bool:
    """Whether face analytics may run at all.

    Read from the environment on every call rather than cached at import, so that
    turning it off takes effect without a rebuild, and so that no import order can
    leave a stale True behind.

    There is no truthy default. A missing variable, an empty one, or anything that
    is not an explicit affirmative means off.
    """
    return os.environ.get("FRS_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FaceDetection:
    """A face-shaped region. Not a person, and not an identity."""

    x: int
    y: int
    width: int
    height: int
    #: The cascade's own level weight for this region, reported unscaled.
    #:
    #: Deliberately **not** squeezed into 0–1. A number between 0 and 1 printed
    #: beside a face reads as "probability this is a person", and any such
    #: reading of this module would be wrong — there is no identity here to be
    #: probable about. An unbounded detector score cannot be mistaken for one.
    #:
    #: (The first version of this did normalise, and every detection came back
    #: at exactly 1.00, which is the other reason not to: a figure that is
    #: always the same conveys nothing while looking as though it does.)
    level_weight: float


class FaceAnalytic:
    """Detects face-shaped regions in a frame. Reports; never identifies.

    Shaped like `VehicleTracker` and `PlateLocator` in `services/anpr/models.py`
    on purpose — one method, frame in, detections out. That similarity *is* the
    demonstration: a second analytic is a class with the same shape, not a
    change to the pipeline that hosts it.
    """

    def __init__(self, cascade_path: str | None = None) -> None:
        self._cascade: Any = None
        self._path = cascade_path or self._find_cascade()
        self.unavailable_reason: str | None = None

        if not frs_enabled():
            self.unavailable_reason = "FRS_ENABLED is not set"
            return
        if self._path is None:
            self.unavailable_reason = (
                "no frontal-face cascade found; install opencv-data or pass a path"
            )
            return
        try:
            import cv2

            cascade = cv2.CascadeClassifier(self._path)
            if cascade.empty():
                self.unavailable_reason = f"cascade at {self._path} failed to load"
                return
            self._cascade = cascade
        except ImportError as exc:  # pragma: no cover - cv2 is present in the image
            self.unavailable_reason = f"OpenCV unavailable: {exc}"

        if self.available:
            log.warning(
                "face analytics ENABLED — detection only, no identification. "
                "Synthetic or consented faces only; see services/analytics/frs.py"
            )

    @staticmethod
    def _find_cascade() -> str | None:
        for candidate in CASCADE_CANDIDATES:
            if pathlib.Path(candidate).is_file():
                return candidate
        return None

    @property
    def available(self) -> bool:
        return self._cascade is not None

    def detect(self, frame: Any) -> list[FaceDetection]:
        """Face-shaped regions in this frame. Empty when disabled or unavailable.

        Returning empty rather than raising is the correct failure here: this is
        an *optional* analytic sharing a worker with the mandatory one, and a
        module that cannot run must not be able to stop plates being read.
        """
        if self._cascade is None:
            return []

        import cv2

        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        height = grey.shape[0]
        min_side = max(16, int(height * MIN_FACE_FRACTION))

        boxes, _levels, weights = self._cascade.detectMultiScale3(
            grey,
            scaleFactor=1.1,
            minNeighbors=MIN_NEIGHBOURS,
            minSize=(min_side, min_side),
            outputRejectLevels=True,
        )
        return [
            FaceDetection(
                x=int(x), y=int(y), width=int(w), height=int(h),
                level_weight=round(float(weight), 3),
            )
            for (x, y, w, h), weight in zip(boxes, weights, strict=True)
        ]
