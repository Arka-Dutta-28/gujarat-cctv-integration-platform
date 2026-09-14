"""Composite vehicle detection: learned detector first, motion when it is blind.

The behaviour worth protecting is that a camera where the CNN sees nothing —
an unusual mounting, a scene outside its training distribution, a synthetic
feed — keeps producing plates instead of silently reporting no traffic, and
that the switch is visible rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from services.anpr.backends.motion import CompositeVehicleTracker
from services.anpr.models import Box, VehicleDetection


def detection(track_id: str = "1") -> VehicleDetection:
    return VehicleDetection(box=Box(0, 0, 50, 50), track_id=track_id, label="car", confidence=0.8)


@dataclass
class Stub:
    results: list = field(default_factory=list)
    calls: int = 0
    resets: int = 0

    def track(self, frame: object) -> list:  # noqa: ARG002
        self.calls += 1
        return list(self.results)

    def reset(self) -> None:
        self.resets += 1


class TestComposite:
    def test_a_productive_detector_is_never_replaced(self) -> None:
        primary, fallback = Stub([detection()]), Stub([detection("m1")])
        c = CompositeVehicleTracker(primary, fallback, patience=3)
        for _ in range(50):
            c.track(object())
        assert fallback.calls == 0
        assert c.fell_back is False

    def test_a_blind_detector_is_replaced_after_patience_runs_out(self) -> None:
        primary, fallback = Stub([]), Stub([detection("m1")])
        c = CompositeVehicleTracker(primary, fallback, patience=3)
        for _ in range(3):
            c.track(object())
        assert c.fell_back is True
        assert c.track(object())[0].track_id == "m1"

    def test_an_occasional_empty_frame_is_not_a_blind_detector(self) -> None:
        """Most frames of most cameras genuinely contain no vehicle."""
        primary, fallback = Stub([]), Stub([detection("m1")])
        c = CompositeVehicleTracker(primary, fallback, patience=5)
        for i in range(30):
            primary.results = [] if i % 4 else [detection()]
            c.track(object())
        assert c.fell_back is False

    def test_once_switched_it_stays_switched(self) -> None:
        """Flapping between two detectors would produce two id spaces."""
        primary, fallback = Stub([]), Stub([detection("m1")])
        c = CompositeVehicleTracker(primary, fallback, patience=2)
        c.track(object())
        c.track(object())
        primary.results = [detection()]
        assert c.track(object())[0].track_id == "m1"

    def test_a_reset_restores_the_learned_detector(self) -> None:
        primary, fallback = Stub([]), Stub([])
        c = CompositeVehicleTracker(primary, fallback, patience=1)
        c.track(object())
        c.reset()
        assert c.fell_back is False
        assert primary.resets == 1 and fallback.resets == 1


class TestProposalDownscaling:
    """Motion proposals are found on a downscaled copy, reported at full scale.

    The saving is the point — MOG2, the morphological close and the contour
    pass all scale with pixel count, and on the live estate `vehicle_detect`
    was the largest single consumer of a CPU-bound box at 1080p. The risk the
    optimisation introduces is a coordinate-space mistake: a box in
    small-frame units would crop the plate from the wrong part of the vehicle
    and quietly halve the read rate. So what is asserted here is the contract,
    not the speed — every box leaves in full-frame coordinates.
    """

    def _moving_frames(self, width: int, height: int):  # noqa: ANN202
        import numpy as np

        background = np.zeros((height, width, 3), dtype=np.uint8)
        moving = background.copy()
        # A bright block in the lower-right quadrant, large enough to clear
        # MIN_AREA_FRACTION but well inside MAX_AREA_FRACTION.
        y0, x0 = int(height * 0.55), int(width * 0.55)
        y1, x1 = int(height * 0.90), int(width * 0.90)
        moving[y0:y1, x0:x1] = 255
        return background, moving, (x0, y0, x1, y1)

    def test_boxes_are_reported_in_full_frame_coordinates(self) -> None:
        pytest.importorskip("cv2")
        from services.anpr.backends.motion import MotionVehicleTracker

        width, height = 1920, 1080
        background, moving, (x0, y0, x1, y1) = self._moving_frames(width, height)

        tracker = MotionVehicleTracker()
        for _ in range(20):
            tracker.track(background)
        detections = tracker.track(moving)

        assert detections, "a large moving block should be proposed as a vehicle"
        box = max((d.box for d in detections), key=lambda b: b.area)

        # Full-frame coordinates, not the 640-wide working copy. The loose
        # tolerance absorbs the morphological close and the rescale rounding.
        assert box.x2 > 640, "box looks like it is still in downscaled units"
        assert abs(box.x1 - x0) < width * 0.08
        assert abs(box.y1 - y0) < height * 0.08
        assert abs(box.x2 - x1) < width * 0.08
        assert abs(box.y2 - y1) < height * 0.08

    def test_a_frame_narrower_than_the_working_width_is_left_alone(self) -> None:
        pytest.importorskip("cv2")
        from services.anpr.backends.motion import MotionVehicleTracker

        width, height = 320, 240
        background, moving, (x0, y0, _, _) = self._moving_frames(width, height)

        tracker = MotionVehicleTracker()
        for _ in range(20):
            tracker.track(background)
        detections = tracker.track(moving)

        assert detections
        box = max((d.box for d in detections), key=lambda b: b.area)
        assert box.x2 <= width and box.y2 <= height
        assert abs(box.x1 - x0) < width * 0.10
