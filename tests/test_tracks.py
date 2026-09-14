"""Track lifecycle: one sighting per tracked vehicle, written in good time.

The rule this protects is the per-track voting convention. The failure
modes are opposite and both bad: emit per frame and `sightings` fills with
duplicates of the same car; hold a track open too long and a watchlist hit
lands after the vehicle has gone.
"""

from __future__ import annotations

from services.anpr.tracks import TrackRegistry
from services.anpr.vote import PlateRead


def read(text: str, conf: float = 0.8) -> PlateRead:
    return PlateRead(raw=text, confidence=conf)


class TestOneSightingPerVehicle:
    def test_a_vehicle_crossing_the_frame_produces_exactly_one_sighting(self) -> None:
        reg = TrackRegistry("cam")
        for i in range(20):
            reg.observe("1", now=i * 0.1, read=read("GJ01AB1234"))
        assert reg.harvest(now=2.0) == []      # still in frame
        done = reg.harvest(now=5.0)            # gone
        assert len(done) == 1
        assert done[0].result is not None
        assert done[0].result.plate_normalised == "GJ01AB1234"
        assert done[0].result.read_count == 20

    def test_two_vehicles_produce_two_sightings(self) -> None:
        reg = TrackRegistry("cam")
        for i in range(10):
            reg.observe("1", now=i * 0.1, read=read("GJ01AB1234"))
            reg.observe("2", now=i * 0.1, read=read("GJ05CD5678"))
        done = reg.harvest(now=5.0)
        assert {d.result.plate_normalised for d in done if d.result} == {
            "GJ01AB1234", "GJ05CD5678",
        }

    def test_a_tracked_vehicle_with_no_readable_plate_is_still_counted(self) -> None:
        """The honest denominator for any accuracy claim."""
        reg = TrackRegistry("cam")
        reg.observe("1", now=0.0, vehicle_class="car")
        done = reg.harvest(now=5.0)
        assert len(done) == 1
        assert done[0].result is None
        assert done[0].track.vehicle_class == "car"


class TestTimeliness:
    def test_a_parked_vehicle_does_not_hold_its_sighting_for_ever(self) -> None:
        reg = TrackRegistry("cam", max_duration_s=10.0)
        for i in range(200):
            reg.observe("1", now=i * 0.1, read=read("GJ01AB1234"))
            reg.harvest(now=i * 0.1)
        assert reg.completed >= 1

    def test_a_cut_track_keeps_tracking_the_same_vehicle(self) -> None:
        reg = TrackRegistry("cam", max_duration_s=5.0)
        for i in range(100):
            reg.observe("1", now=i * 0.1, read=read("GJ01AB1234"))
            reg.harvest(now=i * 0.1)
        assert "1" in reg.tracks

    def test_a_brief_occlusion_does_not_split_one_vehicle_into_two(self) -> None:
        reg = TrackRegistry("cam", idle_timeout_s=2.0)
        reg.observe("1", now=0.0, read=read("GJ01AB1234"))
        assert reg.harvest(now=1.5) == []   # behind a pole
        reg.observe("1", now=1.6, read=read("GJ01AB1234"))
        assert len(reg.harvest(now=4.0)) == 1


class TestEvidence:
    def test_geometry_comes_from_the_frame_that_read_best(self) -> None:
        """The crop in the report should be the legible one, not the last one."""
        reg = TrackRegistry("cam")
        reg.observe("1", now=0.0, read=read("GJ01AB1234", 0.95),
                    bbox=(10, 10, 100, 100), plate_bbox=(40, 60, 70, 72))
        reg.observe("1", now=0.1, read=read("GJ01A81234", 0.30),
                    bbox=(999, 999, 999, 999), plate_bbox=(999, 999, 999, 999))
        done = reg.harvest(now=5.0)
        assert done[0].track.bbox == (10, 10, 100, 100)
        assert done[0].track.plate_bbox == (40, 60, 70, 72)

    def test_a_stopped_stream_flushes_what_it_had(self) -> None:
        reg = TrackRegistry("cam")
        reg.observe("1", now=0.0, read=read("GJ01AB1234"))
        done = reg.flush()
        assert len(done) == 1
        assert done[0].reason == "stream ended"
        assert reg.active == 0

    def test_the_condition_is_carried_onto_the_sighting(self) -> None:
        reg = TrackRegistry("cam")
        reg.observe("1", now=0.0, read=read("GJ01AB1234"), condition="glare")
        assert reg.harvest(now=5.0)[0].track.condition == "glare"


class TestReadBudget:
    """OCR is the most expensive stage; the vote saturates. Stop paying."""

    def test_a_new_track_always_wants_reads(self) -> None:
        reg = TrackRegistry("cam")
        t = reg.observe("1", now=0.0)
        assert t.needs_more_reads() is True

    def test_a_settled_track_stops_asking(self) -> None:
        reg = TrackRegistry("cam")
        for i in range(3):
            reg.observe("1", now=i * 0.1, read=read("GJ01AB1234", 0.9))
        assert reg.tracks["1"].needs_more_reads() is False

    def test_disagreeing_reads_keep_it_asking(self) -> None:
        """A disputed plate is exactly the one worth another look."""
        reg = TrackRegistry("cam")
        for plate in ("GJ01AB1234", "GJ01AB1284", "GJ01A81234"):
            reg.observe("1", now=0.1, read=read(plate, 0.9))
        assert reg.tracks["1"].needs_more_reads() is True

    def test_low_confidence_agreement_keeps_it_asking(self) -> None:
        reg = TrackRegistry("cam")
        for _ in range(3):
            reg.observe("1", now=0.1, read=read("GJ01AB1234", 0.4))
        assert reg.tracks["1"].needs_more_reads() is True

    def test_the_budget_is_hard_capped_even_when_never_settled(self) -> None:
        """A plate that never agrees must not consume unbounded OCR."""
        reg = TrackRegistry("cam")
        for i in range(12):
            reg.observe("1", now=0.1, read=read(f"GJ01AB{1000 + i}", 0.4))
        assert reg.tracks["1"].needs_more_reads() is False

    def test_a_settled_track_still_produces_its_sighting(self) -> None:
        """Stopping OCR must not stop the vehicle being recorded."""
        reg = TrackRegistry("cam")
        for i in range(3):
            reg.observe("1", now=i * 0.1, read=read("GJ01AB1234", 0.9))
        for i in range(20):
            reg.observe("1", now=0.4 + i * 0.1)
        done = reg.harvest(now=10.0)
        assert done[0].result is not None
        assert done[0].result.plate_normalised == "GJ01AB1234"
        assert done[0].track.frames == 23
