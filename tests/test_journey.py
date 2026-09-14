"""Journey reconstruction: visits, hops, legs and honest confidence.

The behaviours worth protecting, in the order they matter:

1. An impossible transition is *kept and flagged*, never dropped. It is how a
   cloned plate surfaces, and a trace that silently discarded it would report a
   clean journey for the one vehicle an investigator most wants to know about.
2. Consecutive sightings on one camera are one visit. A vehicle held in view
   produces a new track every 30 seconds by design, and rendering those as
   separate points draws a car teleporting on the spot.
3. Confidence describes the whole route, not the average read. Confident reads
   describing a physically impossible path are not a confident route.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.common.geo import IMPLAUSIBLE_SPEED_KMH
from services.journey import collapse_revisits, reconstruct

BASE = datetime(2026, 8, 18, 9, 0, tzinfo=UTC)

# Two points ~9.4 km apart along NH-48.
AHMEDABAD = (23.0225, 72.5714)
NEARBY = (23.1000, 72.6000)
FAR = (21.1702, 72.8311)  # Surat, ~230 km away


def sighting(
    sid: int, camera: str, minutes: float, lat: float, lon: float, confidence: float = 0.8
) -> dict:
    return {
        "id": sid,
        "camera_id": camera,
        "camera_name": f"cam {camera}",
        "district": "Ahmedabad",
        "lat": lat,
        "lon": lon,
        "ts": BASE + timedelta(minutes=minutes),
        "confidence": confidence,
    }


class TestCollapseRevisits:
    def test_consecutive_sightings_on_one_camera_become_one_visit(self) -> None:
        rows = [sighting(i, "cam-01", i * 0.5, *AHMEDABAD) for i in range(6)]
        visits = collapse_revisits(rows)

        assert len(visits) == 1
        assert visits[0].sightings == 6
        assert visits[0].dwell_s == pytest.approx(2.5 * 60)
        assert visits[0].sighting_ids == [0, 1, 2, 3, 4, 5]

    def test_a_gap_longer_than_the_window_is_a_second_visit(self) -> None:
        rows = [
            sighting(1, "cam-01", 0, *AHMEDABAD),
            sighting(2, "cam-01", 30, *AHMEDABAD),  # half an hour later
        ]
        visits = collapse_revisits(rows)
        assert len(visits) == 2, "a vehicle that left and came back is two visits"

    def test_a_different_camera_always_starts_a_new_visit(self) -> None:
        rows = [
            sighting(1, "cam-01", 0, *AHMEDABAD),
            sighting(2, "cam-02", 0.1, *NEARBY),
        ]
        assert len(collapse_revisits(rows)) == 2

    def test_visit_confidence_is_the_best_read_not_the_mean(self) -> None:
        rows = [
            sighting(1, "cam-01", 0, *AHMEDABAD, confidence=0.30),
            sighting(2, "cam-01", 0.5, *AHMEDABAD, confidence=0.95),
            sighting(3, "cam-01", 1.0, *AHMEDABAD, confidence=0.20),
        ]
        assert collapse_revisits(rows)[0].confidence == pytest.approx(0.95)

    def test_a_camera_without_coordinates_is_skipped_not_invented(self) -> None:
        rows = [sighting(1, "cam-01", 0, *AHMEDABAD), sighting(2, "cam-02", 5, *NEARBY)]
        rows[1]["lat"] = None
        visits = collapse_revisits(rows)
        assert [v.camera_id for v in visits] == ["cam-01"]


class TestPlausibility:
    def test_a_normal_drive_is_plausible(self) -> None:
        # ~9.4 km in 10 minutes is about 56 km/h.
        rows = [
            sighting(1, "cam-01", 0, *AHMEDABAD),
            sighting(2, "cam-02", 10, *NEARBY),
        ]
        journey = reconstruct("GJ01AB1234", rows)
        assert len(journey.hops) == 1
        assert journey.hops[0].plausible
        assert journey.hops[0].note is None
        assert len(journey.legs) == 1

    def test_an_impossible_hop_is_flagged_and_kept(self) -> None:
        # ~230 km in 10 minutes is well over 1,000 km/h.
        rows = [
            sighting(1, "cam-01", 0, *AHMEDABAD),
            sighting(2, "cam-99", 10, *FAR),
        ]
        journey = reconstruct("GJ01AB1234", rows)

        assert not journey.hops[0].plausible
        assert "cloned plate" in journey.hops[0].note
        # The invariant: nothing was dropped.
        assert len(journey.visits) == 2
        assert journey.implausible_hops

    def test_an_impossible_hop_splits_the_journey_into_legs(self) -> None:
        rows = [
            sighting(1, "cam-01", 0, *AHMEDABAD),
            sighting(2, "cam-02", 10, *NEARBY),
            sighting(3, "cam-99", 15, *FAR),
        ]
        journey = reconstruct("GJ01AB1234", rows)

        assert len(journey.legs) == 2
        assert [v.camera_id for v in journey.legs[0].visits] == ["cam-01", "cam-02"]
        assert [v.camera_id for v in journey.legs[1].visits] == ["cam-99"]

    def test_two_cameras_at_the_same_instant_are_implausible_with_a_reason(self) -> None:
        rows = [
            sighting(1, "cam-01", 0, *AHMEDABAD),
            sighting(2, "cam-99", 0, *FAR),
        ]
        hop = reconstruct("GJ01AB1234", rows).hops[0]
        assert not hop.plausible
        assert "same instant" in hop.note

    def test_co_located_cameras_do_not_trip_the_clone_detector(self) -> None:
        """Two cameras on one junction divide a tiny distance by a tiny time."""
        rows = [
            sighting(1, "cam-01", 0, *AHMEDABAD),
            sighting(2, "cam-02", 0.001, *AHMEDABAD),
        ]
        assert reconstruct("GJ01AB1234", rows).hops[0].plausible

    def test_elapsed_time_is_measured_from_last_seen_not_first_seen(self) -> None:
        """Dwell belongs to the camera, not to the drive that follows it."""
        # Both cam-01 reads fall inside REVISIT_WINDOW_S, so they are one visit
        # spanning two minutes; the drive that follows takes another two.
        rows = [
            sighting(1, "cam-01", 0, *AHMEDABAD),
            sighting(2, "cam-01", 2, *AHMEDABAD),   # still dwelling
            sighting(3, "cam-02", 4, *NEARBY),      # then driving
        ]
        journey = reconstruct("GJ01AB1234", rows)
        assert len(journey.visits) == 2, "the two cam-01 reads are one visit"

        hop = journey.hops[0]
        assert hop.elapsed_s == pytest.approx(120, abs=1)
        # Charging the dwell to the drive would have said ~140 km/h against the
        # truth of ~280. Both are implausible here, but the distinction decides
        # borderline cases, which is where a trace is actually contested.
        assert not hop.plausible


class TestConfidence:
    def test_confidence_starts_from_how_well_the_plates_were_read(self) -> None:
        rows = [
            sighting(1, "cam-01", 0, *AHMEDABAD, confidence=0.9),
            sighting(2, "cam-02", 10, *NEARBY, confidence=0.7),
        ]
        assert reconstruct("GJ01AB1234", rows).confidence == pytest.approx(0.8)

    def test_an_implausible_transition_degrades_confidence(self) -> None:
        good = reconstruct("X", [
            sighting(1, "cam-01", 0, *AHMEDABAD, confidence=0.9),
            sighting(2, "cam-02", 10, *NEARBY, confidence=0.9),
        ])
        bad = reconstruct("X", [
            sighting(1, "cam-01", 0, *AHMEDABAD, confidence=0.9),
            sighting(2, "cam-99", 10, *FAR, confidence=0.9),
        ])
        assert bad.confidence < good.confidence
        assert bad.confidence > 0

    def test_confidence_never_goes_negative_however_many_contradictions(self) -> None:
        rows = []
        for i in range(10):
            here = AHMEDABAD if i % 2 == 0 else FAR
            rows.append(sighting(i, f"cam-{i}", i, *here, confidence=0.9))
        journey = reconstruct("X", rows)
        assert 0.0 <= journey.confidence <= 1.0
        assert len(journey.implausible_hops) >= 8


class TestRoadDistances:
    def test_a_supplied_road_distance_is_used_and_flagged(self) -> None:
        rows = [
            sighting(1, "cam-01", 0, *AHMEDABAD),
            sighting(2, "cam-02", 10, *NEARBY),
        ]
        journey = reconstruct("X", rows, road_distances={("cam-01", "cam-02"): 15_000.0})
        hop = journey.hops[0]
        assert hop.road_snapped
        assert hop.road_distance_m == pytest.approx(15_000.0)
        assert hop.distance_m == pytest.approx(15_000.0), "display prefers the road"
        # 15 km in 10 minutes is 90 km/h by road.
        assert hop.road_speed_kmh == pytest.approx(90.0, abs=0.5)
        # But the plausibility speed stays on the direct line: ~9.4 km, ~56 km/h.
        assert hop.implied_speed_kmh == pytest.approx(56.0, abs=2.0)

    def test_a_missing_pair_falls_back_per_segment_not_wholesale(self) -> None:
        rows = [
            sighting(1, "cam-01", 0, *AHMEDABAD),
            sighting(2, "cam-02", 10, *NEARBY),
            sighting(3, "cam-03", 20, *AHMEDABAD),
        ]
        journey = reconstruct("X", rows, road_distances={("cam-01", "cam-02"): 15_000.0})
        assert [h.road_snapped for h in journey.hops] == [True, False]


class TestEmptyAndSingle:
    def test_no_sightings_is_an_empty_journey_not_an_error(self) -> None:
        journey = reconstruct("GJ01AB1234", [])
        assert journey.visits == []
        assert journey.hops == []
        assert journey.confidence == 0.0
        assert journey.summary == "No sightings."

    def test_one_sighting_is_a_journey_with_no_hops(self) -> None:
        journey = reconstruct("X", [sighting(1, "cam-01", 0, *AHMEDABAD)])
        assert len(journey.visits) == 1
        assert journey.hops == []
        assert len(journey.legs) == 1
        assert journey.distance_m == 0.0


class TestPlausibilityBasis:
    """A clone accusation must rest on a distance the vehicle certainly covered.

    Snapped road distances on this estate ran 1.5x to 2.1x the direct line,
    because cameras interpolated along a coarse corridor snap to service roads
    and OSRM routes around them. Judging speed on that inflated distance turned
    a correctly-planted 77 km/h journey into a 162 km/h "clone" — a false
    accusation produced entirely by map-matching quality.

    The direct line cannot be wrong in that direction: nothing travels less than
    the straight line, so a speed computed from it is a lower bound, and
    exceeding the limit even on the lower bound is a statement about geometry.
    """

    def test_an_inflated_road_distance_does_not_manufacture_a_clone(self) -> None:
        rows = [
            sighting(1, "cam-01", 0, *AHMEDABAD),
            sighting(2, "cam-02", 10, *NEARBY),
        ]
        # Direct is ~9.4 km (about 56 km/h over ten minutes). A detoured route
        # twice that long implies 113 km/h, and a worse match would exceed 120.
        journey = reconstruct("X", rows, road_distances={("cam-01", "cam-02"): 27_000.0})
        hop = journey.hops[0]

        assert hop.road_speed_kmh > IMPLAUSIBLE_SPEED_KMH
        assert hop.plausible, "plausibility must not depend on map-match quality"
        assert hop.note is None

    def test_a_genuine_clone_is_still_caught_with_road_distances_present(self) -> None:
        rows = [
            sighting(1, "cam-01", 0, *AHMEDABAD),
            sighting(2, "cam-99", 10, *FAR),
        ]
        journey = reconstruct("X", rows, road_distances={("cam-01", "cam-99"): 250_000.0})
        hop = journey.hops[0]
        assert not hop.plausible
        assert "straight line" in hop.note

    def test_both_distances_are_reported(self) -> None:
        rows = [
            sighting(1, "cam-01", 0, *AHMEDABAD),
            sighting(2, "cam-02", 10, *NEARBY),
        ]
        hop = reconstruct("X", rows, road_distances={("cam-01", "cam-02"): 15_000.0}).hops[0]
        assert hop.direct_distance_m < hop.road_distance_m
        assert hop.implied_speed_kmh < hop.road_speed_kmh

    def test_without_a_road_distance_only_the_direct_one_is_reported(self) -> None:
        rows = [
            sighting(1, "cam-01", 0, *AHMEDABAD),
            sighting(2, "cam-02", 10, *NEARBY),
        ]
        hop = reconstruct("X", rows).hops[0]
        assert hop.road_distance_m is None
        assert hop.road_speed_kmh is None
        assert hop.distance_m == hop.direct_distance_m
