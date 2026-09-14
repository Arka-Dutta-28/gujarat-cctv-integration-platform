"""Motion-proposal vehicle detection, for fixed cameras.

Background subtraction finds the moving foreground, contours become candidate
vehicles, and CentroidTracker gives them ids. No model, no GPU, no download.

Why this exists alongside the YOLO backend.

A learned detector is not always the right tool. A CCTV estate is
overwhelmingly fixed cameras looking at a static scene, and on those the moving
region is the vehicle. Running a CNN on every analysed frame to rediscover that
is expensive, and at 80,000 cameras the cost difference is the difference
between a plausible deployment and an implausible one.

A learned detector sometimes sees nothing at all. An unusual mounting, a scene
far from its training distribution or a synthetic test feed can all defeat it.
The platform should keep reading plates rather than go blind, and it should say
in its metrics that it fell back rather than quietly report zero traffic.

The honest limitation, and it is a real one: this proposes moving objects, not
vehicles. It cannot tell a lorry from a pedestrian, so it labels everything
`car` and lets the plate stage decide, since an object with no plate simply
yields no read. Anything measured with this backend must be reported as measured
with it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from services.anpr.models import Box, VehicleDetection
from services.anpr.tracker_core import CentroidTracker

log = logging.getLogger("anpr.motion")

__all__ = ["MotionVehicleTracker", "CompositeVehicleTracker"]

#: Foreground blobs smaller than this fraction of the frame are noise —
#: a bird, a shadow edge, sensor grain.
MIN_AREA_FRACTION = float(os.environ.get("ANPR_MOTION_MIN_AREA", "0.004"))

#: And larger than this is a lighting change, not a vehicle.
MAX_AREA_FRACTION = 0.6

#: Frames of history the background model keeps.
HISTORY = 300

#: Width the background model works at. Motion proposals do not need detail —
#: they need to know *where* something moved, and the plate crop is still taken
#: from the full-resolution frame afterwards. Running MOG2, the morphological
#: close and the contour pass over a full 1080p frame cost `vehicle_detect` p50
#: 59.5 ms across the live estate and was the largest single consumer of a
#: CPU-bound box; the work scales with pixel count, so a half-width copy is
#: roughly a quarter of it. `FrameAnalyser` already downscales to 160 px for the
#: same reason.
PROPOSAL_WIDTH = int(os.environ.get("ANPR_MOTION_WIDTH", "640"))


class MotionVehicleTracker:
    """Proposes moving regions as vehicles and tracks them across frames."""

    def __init__(self, min_area_fraction: float = MIN_AREA_FRACTION) -> None:
        self.min_area_fraction = min_area_fraction
        self.tracker = CentroidTracker()
        self._subtractor: Any = None

    def track(self, frame: Any) -> list[VehicleDetection]:
        import cv2

        if self._subtractor is None:
            self._subtractor = cv2.createBackgroundSubtractorMOG2(
                history=HISTORY, varThreshold=32, detectShadows=False
            )

        height, width = frame.shape[:2]
        area = float(width * height) or 1.0

        # Detect small, report large. Every box found below is scaled back into
        # full-frame coordinates before it leaves this method, so nothing
        # downstream — the tracker, the plate crop, the stored bbox — can tell
        # the difference beyond the rounding.
        if width > PROPOSAL_WIDTH:
            scale = PROPOSAL_WIDTH / width
            small = cv2.resize(
                frame, (PROPOSAL_WIDTH, max(1, int(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        else:
            scale = 1.0
            small = frame

        mask = self._subtractor.apply(small)
        # Close gaps so one vehicle is one blob rather than a windscreen, a
        # bonnet and a bumper tracked separately.
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))
        )
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        boxes: list[Box] = []
        scores: list[float] = []
        inverse = 1.0 / scale
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if scale != 1.0:
                x, y, w, h = (
                    int(x * inverse), int(y * inverse),
                    int(w * inverse), int(h * inverse),
                )
            fraction = (w * h) / area
            if not self.min_area_fraction <= fraction <= MAX_AREA_FRACTION:
                continue
            boxes.append(Box(x, y, x + w, y + h))
            # Not a class probability — how solidly the blob filled its box.
            # Reported as confidence so the vote weights a ragged blob lower.
            # Solidity, so both terms must be in the same coordinate space:
            # contourArea is measured on the downscaled mask.
            box_area_small = (w * scale) * (h * scale) or 1.0
            scores.append(
                round(min(0.4 + cv2.contourArea(contour) / box_area_small * 0.5, 0.9), 3)
            )

        ids = self.tracker.update(boxes, width, height)
        return [
            VehicleDetection(box=box, track_id=track_id, label="car", confidence=score)
            for box, track_id, score in zip(boxes, ids, scores, strict=True)
        ]

    def reset(self) -> None:
        self.tracker.reset()
        self._subtractor = None


class CompositeVehicleTracker:
    """Learned detector first, motion proposals when it finds nothing.

    A camera where the CNN is productive keeps using it. A camera where it has
    returned nothing across a run of analysed frames *that contained motion*
    switches to proposals — the scene is moving, so "no vehicles" is a claim
    about the detector, not about the road.

    The switch is recorded rather than silent: `fell_back` is reported in the
    worker's metrics, because a fallback that nobody can see turns into an
    accuracy figure nobody can explain.
    """

    def __init__(self, primary: Any, fallback: Any, patience: int = 40) -> None:
        self.primary = primary
        self.fallback = fallback
        self.patience = patience
        self.empty_runs = 0
        self.fell_back = False

    def track(self, frame: Any) -> list[VehicleDetection]:
        if self.fell_back:
            return self.fallback.track(frame)

        try:
            detections = self.primary.track(frame)
        except Exception:  # noqa: BLE001 - see below
            # A primary that *raises* is at least as broken as one that finds
            # nothing, and this class exists to survive the second case. Without
            # this it survived only the second: a learned model whose library is
            # missing imports lazily, on the first frame, and the exception went
            # straight past the patience counter to kill the camera.
            log.exception(
                "primary vehicle detector failed; falling back permanently for "
                "this camera"
            )
            self.fell_back = True
            return self.fallback.track(frame)
        if detections:
            self.empty_runs = 0
            return detections

        self.empty_runs += 1
        if self.empty_runs >= self.patience:
            log.warning(
                "no learned detections in %d analysed frames; switching to motion "
                "proposals for this camera", self.empty_runs,
            )
            self.fell_back = True
            return self.fallback.track(frame)
        return []

    def reset(self) -> None:
        self.primary.reset()
        self.fallback.reset()
        self.empty_runs = 0
        self.fell_back = False
