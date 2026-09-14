"""Vehicle ids: which earlier sighting a new one is joined to, and when it is not."""

from __future__ import annotations

from services.anpr.linking import Candidate, Link, choose


def cand(uid: int, distance: float | None, *, seconds: float = 600, metres: float | None = 2_000,
         plate: str = "", identifying: bool = False, cls: str | None = "car") -> Candidate:
    return Candidate(uid, seconds, metres, distance, plate, identifying, cls)


def test_a_readable_plate_joins_the_vehicle_that_carried_it() -> None:
    link = choose(9, "GJ01AB1234", True, "car", [
        cand(1, 0.02), cand(2, None, plate="GJ01AB1234", identifying=True),
    ])
    assert link == Link(2, "plate")


def test_a_clear_nearest_look_alike_is_joined_with_its_distance() -> None:
    link = choose(9, "", False, "car", [cand(1, 0.03), cand(2, 0.09)])
    assert link == Link(1, "appearance", 0.03)


def test_two_equally_close_vehicles_are_refused_not_guessed() -> None:
    assert choose(9, "", False, "car", [cand(1, 0.03), cand(2, 0.04)]) == Link(9, "new")


def test_rows_of_one_vehicle_count_once_when_judging_the_margin() -> None:
    assert choose(9, "", False, "car", [cand(1, 0.03), cand(1, 0.04)]).via == "appearance"


def test_too_far_to_look_like_it_is_a_new_vehicle() -> None:
    assert choose(9, "", False, "car", [cand(1, 0.08)]) == Link(9, "new")


def test_a_hop_faster_than_120_kmh_is_not_the_same_vehicle() -> None:
    assert choose(9, "", False, "car", [cand(1, 0.02, seconds=60, metres=5_000)]).via == "new"


def test_a_car_is_never_joined_to_a_two_wheeler_or_to_a_different_plate() -> None:
    assert choose(9, "", False, "car", [cand(1, 0.02, cls="motorcycle")]).via == "new"
    assert choose(9, "GJ01AB1234", True, "car", [
        cand(1, 0.02, plate="GJ05UV9972", identifying=True),
    ]).via == "new"
    assert choose(9, "", False, "car", [cand(1, 0.02, cls="truck")]).via == "appearance"
