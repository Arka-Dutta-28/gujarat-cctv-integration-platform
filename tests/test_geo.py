from __future__ import annotations

import math

import pytest

from services.common.geo import (
    IMPLAUSIBLE_SPEED_KMH,
    Point,
    haversine_m,
    implied_speed_kmh,
    initial_bearing_deg,
    interpolate_polyline,
    is_plausible_transition,
    polyline_length_m,
)

AHMEDABAD = Point(23.0225, 72.5714)
VADODARA = Point(22.3072, 73.1812)
SURAT = Point(21.1702, 72.8311)


class TestHaversine:
    def test_known_distance_ahmedabad_vadodara(self) -> None:
        # ~101 km straight line; the road is longer, which is exactly why M4
        # snaps to OSRM rather than trusting this number for travel time.
        assert haversine_m(AHMEDABAD, VADODARA) == pytest.approx(101_000, rel=0.03)

    def test_zero_for_identical_points(self) -> None:
        assert haversine_m(AHMEDABAD, AHMEDABAD) == 0.0

    def test_symmetric(self) -> None:
        assert haversine_m(AHMEDABAD, SURAT) == pytest.approx(haversine_m(SURAT, AHMEDABAD))


class TestBearing:
    def test_due_north(self) -> None:
        assert initial_bearing_deg(Point(0, 0), Point(1, 0)) == 0

    def test_due_east(self) -> None:
        assert initial_bearing_deg(Point(0, 0), Point(0, 1)) == 90

    def test_due_south(self) -> None:
        assert initial_bearing_deg(Point(1, 0), Point(0, 0)) == 180

    def test_corridor_runs_broadly_south(self) -> None:
        b = initial_bearing_deg(AHMEDABAD, SURAT)
        assert 170 <= b <= 220

    def test_always_in_range(self) -> None:
        b = initial_bearing_deg(Point(0, 1), Point(0, 0))
        assert 0 <= b <= 359


class TestInterpolate:
    def test_returns_requested_count(self) -> None:
        pts = interpolate_polyline([AHMEDABAD, VADODARA, SURAT], 50)
        assert len(pts) == 50

    def test_spacing_is_even_by_distance(self) -> None:
        """The reason for interpolating by distance rather than by waypoint."""
        line = [AHMEDABAD, VADODARA, SURAT]
        pts = interpolate_polyline(line, 20)
        gaps = [haversine_m(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
        assert max(gaps) / min(gaps) < 1.15

    def test_no_point_sits_on_a_terminus(self) -> None:
        pts = interpolate_polyline([AHMEDABAD, SURAT], 10)
        assert haversine_m(pts[0], AHMEDABAD) > 0
        assert haversine_m(pts[-1], SURAT) > 0

    def test_stays_within_corridor_length(self) -> None:
        line = [AHMEDABAD, VADODARA, SURAT]
        pts = interpolate_polyline(line, 30)
        assert polyline_length_m(pts) <= polyline_length_m(line) + 1

    def test_degenerate_inputs(self) -> None:
        assert interpolate_polyline([], 5) == []
        assert interpolate_polyline([AHMEDABAD], 3) == [AHMEDABAD] * 3
        assert interpolate_polyline([AHMEDABAD, SURAT], 0) == []


class TestPlausibility:
    def test_highway_speed_is_plausible(self) -> None:
        # 5 km in 4 minutes = 75 km/h
        assert is_plausible_transition(5_000, 240) is True

    def test_cloned_plate_speed_is_not(self) -> None:
        # 100 km in 10 minutes = 600 km/h — two vehicles, one plate.
        assert is_plausible_transition(100_000, 600) is False

    def test_boundary_is_inclusive(self) -> None:
        # Distance covered in exactly one second at the limit.
        one_second_at_limit_m = IMPLAUSIBLE_SPEED_KMH * 1000 / 3600
        assert is_plausible_transition(one_second_at_limit_m, 1) is True

    def test_just_over_the_limit_is_flagged(self) -> None:
        one_second_at_limit_m = IMPLAUSIBLE_SPEED_KMH * 1000 / 3600
        assert is_plausible_transition(one_second_at_limit_m * 1.01, 1) is False

    def test_simultaneous_sightings_are_implausible_not_a_crash(self) -> None:
        assert implied_speed_kmh(1000, 0) == math.inf
        assert is_plausible_transition(1000, 0) is False

    def test_stationary_vehicle(self) -> None:
        assert implied_speed_kmh(0, 600) == 0.0
