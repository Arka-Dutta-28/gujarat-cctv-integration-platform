"""IoU/centroid association.

This is what keeps one vehicle as one track when the learned detector is not
in play. Getting it wrong produces the same damage as a bad tracker anywhere
else in the pipeline: one car becomes several sightings seconds apart, or two
cars are merged and their plates vote against each other.
"""

from __future__ import annotations

from services.anpr.models import Box
from services.anpr.tracker_core import CentroidTracker, centre_distance, iou


class TestGeometry:
    def test_identical_boxes_fully_overlap(self) -> None:
        assert iou(Box(0, 0, 10, 10), Box(0, 0, 10, 10)) == 1.0

    def test_disjoint_boxes_do_not_overlap(self) -> None:
        assert iou(Box(0, 0, 10, 10), Box(50, 50, 60, 60)) == 0.0

    def test_touching_edges_are_not_an_overlap(self) -> None:
        assert iou(Box(0, 0, 10, 10), Box(10, 0, 20, 10)) == 0.0

    def test_half_overlap_is_a_third_by_union(self) -> None:
        assert abs(iou(Box(0, 0, 10, 10), Box(5, 0, 15, 10)) - 1 / 3) < 1e-9

    def test_centre_distance_is_euclidean(self) -> None:
        assert centre_distance(Box(0, 0, 10, 10), Box(0, 0, 10, 10)) == 0.0
        assert centre_distance(Box(0, 0, 10, 10), Box(30, 0, 40, 10)) == 30.0


class TestAssociation:
    def test_a_vehicle_keeps_its_id_as_it_moves(self) -> None:
        t = CentroidTracker()
        first = t.update([Box(100, 100, 200, 200)], 1920, 1080)
        second = t.update([Box(115, 105, 215, 205)], 1920, 1080)
        assert first == second

    def test_two_vehicles_get_two_ids_and_keep_them(self) -> None:
        t = CentroidTracker()
        a = t.update([Box(100, 100, 200, 200), Box(800, 100, 900, 200)], 1920, 1080)
        assert len(set(a)) == 2
        b = t.update([Box(110, 100, 210, 200), Box(810, 100, 910, 200)], 1920, 1080)
        assert a == b

    def test_crossing_vehicles_are_not_swapped(self) -> None:
        """The classic association failure: two plates voting into each other."""
        t = CentroidTracker()
        a = t.update([Box(100, 100, 200, 200), Box(400, 100, 500, 200)], 1920, 1080)
        b = t.update([Box(140, 100, 240, 200), Box(360, 100, 460, 200)], 1920, 1080)
        assert a == b

    def test_a_new_vehicle_entering_gets_a_new_id(self) -> None:
        t = CentroidTracker()
        first = t.update([Box(100, 100, 200, 200)], 1920, 1080)
        both = t.update([Box(105, 100, 205, 200), Box(1500, 700, 1600, 800)], 1920, 1080)
        assert both[0] == first[0]
        assert both[1] != first[0]

    def test_a_vehicle_that_jumps_across_the_frame_is_not_the_same_vehicle(self) -> None:
        t = CentroidTracker()
        first = t.update([Box(50, 50, 150, 150)], 1920, 1080)
        second = t.update([Box(1700, 900, 1800, 1000)], 1920, 1080)
        assert first != second

    def test_a_brief_miss_does_not_end_the_track(self) -> None:
        t = CentroidTracker(max_misses=5)
        first = t.update([Box(100, 100, 200, 200)], 1920, 1080)
        for _ in range(3):
            t.update([], 1920, 1080)
        again = t.update([Box(120, 100, 220, 200)], 1920, 1080)
        assert again == first

    def test_a_long_absence_does_end_it(self) -> None:
        t = CentroidTracker(max_misses=2)
        first = t.update([Box(100, 100, 200, 200)], 1920, 1080)
        for _ in range(5):
            t.update([], 1920, 1080)
        again = t.update([Box(100, 100, 200, 200)], 1920, 1080)
        assert again != first

    def test_reset_forgets_everything(self) -> None:
        t = CentroidTracker()
        t.update([Box(100, 100, 200, 200)], 1920, 1080)
        t.reset()
        assert t.tracks == {}
