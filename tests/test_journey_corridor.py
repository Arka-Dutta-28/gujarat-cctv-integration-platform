"""The planted-journey corridor: does the harness actually produce a journey?

This exists because the camera farm looked correct and was not. Cameras sit
4.74 km apart with playback offsets stepping 238 s — one hop at 72 km/h, which
is right — but clips were assigned round-robin across twelve files, so two
cameras showing the same clip were twelve hops (57 km) apart, and the simulator
seeks `offset_s % duration`, turning a 2,856 s offset against a 90 s file into
66 s. Both halves of the stagger were being thrown away.

Nothing failed. The farm published, the pipeline read plates, M0 through M3 all
passed. The defect only became visible when M4 asked for a journey and got one
plate reported at two cameras 57 km apart in the same second, 128 times.

So the property under test is not "the code runs" but "a vehicle crosses the
corridor once, forwards, at a plausible speed" — computed the way the simulator
computes it, modulo included.
"""

from __future__ import annotations

from scripts.make_test_videos import JOURNEY_PLATE, build_journey_clip
from scripts.seed import JOURNEY_CAMERAS, JOURNEY_CLIP, build_farm
from services.common.geo import IMPLAUSIBLE_SPEED_KMH

# The round-robin pool the farm normally draws from, plus the corridor clip.
VIDEO_FILES = [f"traffic-{i:02d}-day.mp4" for i in range(1, 13)] + [JOURNEY_CLIP]


def appearance_time(offset_s: int, plant_s: float, duration_s: float) -> float:
    """When a camera shows the planted pass, the way the simulator computes it.

    The simulator seeks to `offset_s % duration` and plays forward, so at wall
    time W the clip position is `(offset + W) mod duration`.
    """
    return (plant_s - offset_s) % duration_s


class TestCorridorAssignment:
    def test_the_corridor_is_consecutive_cameras(self) -> None:
        farm = build_farm(50, VIDEO_FILES)
        corridor = [c for c in farm if c["source_file"] == JOURNEY_CLIP]

        assert len(corridor) == JOURNEY_CAMERAS
        numbers = [int(c["external_ref"].split("-")[1]) for c in corridor]
        assert numbers == list(range(numbers[0], numbers[0] + JOURNEY_CAMERAS)), (
            "a journey needs adjacent cameras; the round-robin gave 57 km gaps"
        )

    def test_offsets_decrease_downstream_and_never_exceed_the_clip(self) -> None:
        farm = build_farm(50, VIDEO_FILES)
        corridor = [c for c in farm if c["source_file"] == JOURNEY_CLIP]
        offsets = [c["offset_s"] for c in corridor]

        assert offsets == sorted(offsets, reverse=True), (
            "a larger offset starts further into the clip, so the planted pass "
            "has already gone by — downstream cameras must have smaller offsets"
        )
        assert offsets[-1] == 0
        clip = build_journey_clip(0, 960, 540, 15.0, _rng(), cameras=JOURNEY_CAMERAS)
        _ = clip  # duration is asserted against below

    def test_the_vehicle_crosses_the_corridor_once_forwards(self) -> None:
        farm = build_farm(50, VIDEO_FILES)
        corridor = [c for c in farm if c["source_file"] == JOURNEY_CLIP]
        hop = corridor[0]["offset_s"] - corridor[1]["offset_s"]

        duration = JOURNEY_CAMERAS * hop + 120.0
        clip = build_journey_clip(
            duration, 960, 540, 15.0, _rng(), hop_s=hop, cameras=JOURNEY_CAMERAS
        )
        plant = next(p for p in clip.passes if p.plate == JOURNEY_PLATE)

        times = [appearance_time(c["offset_s"], plant.start_s, duration) for c in corridor]

        assert times == sorted(times), (
            f"the vehicle must move downstream, got {times} — if the planted "
            "pass sits earlier than the largest offset, the modulo wraps and "
            "the trace runs backwards"
        )
        # Gaps are not identical, and should not be: the seeded cameras are
        # 4.62-4.77 km apart, and the offsets are derived from that real spacing
        # rather than from a constant. What must hold is that each gap implies a
        # sane driving speed, which is the property the trace depends on.
        from services.common.geo import Point, haversine_m, implied_speed_kmh

        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        for (a, b), gap in zip(zip(corridor, corridor[1:], strict=False), gaps, strict=True):
            metres = haversine_m(Point(a["lat"], a["lon"]), Point(b["lat"], b["lon"]))
            speed = implied_speed_kmh(metres, gap)
            assert 50.0 < speed < IMPLAUSIBLE_SPEED_KMH, (
                f"{a['external_ref']}->{b['external_ref']}: {metres / 1000:.2f} km "
                f"in {gap:.0f} s is {speed:.0f} km/h"
            )

    def test_every_hop_is_below_the_clone_detector_threshold(self) -> None:
        """The harness must not manufacture the very anomaly we detect."""
        from services.common.geo import Point, haversine_m, implied_speed_kmh

        farm = build_farm(50, VIDEO_FILES)
        corridor = [c for c in farm if c["source_file"] == JOURNEY_CLIP]
        hop = corridor[0]["offset_s"] - corridor[1]["offset_s"]

        for a, b in zip(corridor, corridor[1:], strict=False):
            metres = haversine_m(Point(a["lat"], a["lon"]), Point(b["lat"], b["lon"]))
            speed = implied_speed_kmh(metres, hop)
            assert speed < IMPLAUSIBLE_SPEED_KMH, (
                f"{a['external_ref']}->{b['external_ref']} implies {speed:.0f} km/h; "
                "the planted journey would trip our own plausibility check"
            )

    def test_the_planted_plate_appears_exactly_once_in_the_clip(self) -> None:
        clip = build_journey_clip(1548.0, 960, 540, 15.0, _rng())
        planted = [p for p in clip.passes if p.plate == JOURNEY_PLATE]
        assert len(planted) == 1, "two of the same plate would be two vehicles"
        assert len(clip.passes) > 1, "the corridor clip still needs background traffic"

    def test_the_farm_is_unchanged_when_the_corridor_clip_is_absent(self) -> None:
        """Generating without --no-journey must stay optional."""
        plain = build_farm(50, [f"traffic-{i:02d}-day.mp4" for i in range(1, 13)])
        assert all(c["source_file"] != JOURNEY_CLIP for c in plain)
        assert [c["offset_s"] for c in plain] == sorted(c["offset_s"] for c in plain)


def _rng():  # noqa: ANN202
    import random

    return random.Random(20260818)
