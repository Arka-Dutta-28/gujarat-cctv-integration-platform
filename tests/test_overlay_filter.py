"""Burnt-in overlay rejection.

The failure this guards against was measured, not imagined. Across the 31
government cameras the pipeline wrote 422 sightings and zero structurally valid
plates, filling the index instead with RLVD, PTZ1, CSI and 2026: the
violation-detection tag, the camera device label, the system name and the date
stamp. Every one had a plate box at y between 989 and 1066 on a 1080-high frame,
which is the overlay banner along the bottom edge.

Confining OCR to a detected vehicle box did not prevent it. Motion proposals on
live footage are not car-shaped: camera shake, PTZ movement and changing light
produce blobs like {1248,165,1920,1080} spanning a third of the frame, and a
blob reaching the frame edge contains the banner. Worse, the overlay read back
at confidence 0.84, higher than any genuine plate, because a machine renders it
crisply while a real plate is small and weathered, so it also won the per-track
vote.

The property under test is the discriminator, not the mechanism: burnt-in text
appears at the same place with the same characters across many independent
tracks, and a real plate cannot, because it belongs to one vehicle.
"""

from __future__ import annotations

from services.anpr.overlay import StaticTextFilter

# Real coordinates, taken from sightings written by the live government feeds.
RLVD = (1758, 1021, "RLVD")
PTZ1 = (905, 989, "PTZ1")
DATE = (183, 1016, "2026")


class TestOverlayDetection:
    def test_text_recurring_across_many_tracks_becomes_furniture(self) -> None:
        f = StaticTextFilter()
        x, y, text = RLVD

        assert not f.is_static(x, y, text, now=0.0), "nothing is furniture on first sight"

        # Three unrelated motion blobs read it, over a minute, never silent
        # for longer than the filter's memory.
        f.observe(x, y, text, track_id="t1", now=0.0)
        f.observe(x, y, text, track_id="t2", now=30.0)
        f.observe(x, y, text, track_id="t3", now=60.0)

        assert f.is_static(x, y, text, now=60.0)

    def test_one_track_reading_the_same_plate_repeatedly_is_not_furniture(self) -> None:
        """A vehicle held in view is read many times and is still a vehicle."""
        f = StaticTextFilter()
        for i in range(40):
            f.observe(600, 500, "GJ18TR4321", track_id="one-vehicle", now=i * 5.0)
        assert not f.is_static(600, 500, "GJ18TR4321", now=195.0)

    def test_different_plates_in_one_lane_are_not_furniture(self) -> None:
        """The reason position alone is not the discriminator.

        A fixed camera watching one lane sees every plate in roughly the same
        place. Suppressing a hot region would discard exactly the reads we want.
        """
        f = StaticTextFilter()
        for i, plate in enumerate(
            ["GJ01AB1234", "GJ18CD5678", "MH12EF9012", "RJ14GH3456", "GJ05IJ7890"]
        ):
            f.observe(600, 500, plate, track_id=f"track-{i}", now=i * 40.0)
        for plate in ["GJ01AB1234", "GJ18CD5678", "MH12EF9012"]:
            assert not f.is_static(600, 500, plate, now=160.0)

    def test_a_short_lived_burst_is_not_furniture(self) -> None:
        """A convoy of identical fleet markings is traffic; paint persists."""
        f = StaticTextFilter()
        x, y, text = PTZ1
        for i in range(6):
            f.observe(x, y, text, track_id=f"t{i}", now=i * 2.0)  # 10 s total
        assert not f.is_static(x, y, text, now=10.0), "under the persistence threshold"

        # Still being read, without a long silence, a minute later: painted on.
        for i, t in enumerate((40.0, 70.0)):
            f.observe(x, y, text, track_id=f"late-{i}", now=t)
        assert f.is_static(x, y, text, now=70.0), "still there a minute later"

    def test_the_same_text_elsewhere_in_frame_is_judged_separately(self) -> None:
        f = StaticTextFilter()
        x, y, text = DATE
        for i in range(4):
            f.observe(x, y, text, track_id=f"t{i}", now=i * 20.0)
        assert f.is_static(x, y, text, now=60.0)
        # A vehicle genuinely carrying those characters, in a different place.
        assert not f.is_static(600, 400, text, now=60.0)

    def test_positions_within_a_cell_are_the_same_place(self) -> None:
        """The overlay jitters by a pixel or two between frames."""
        f = StaticTextFilter(cell_px=48)
        for i, dx in enumerate([0, 3, 7, 2]):
            f.observe(1758 + dx, 1021, "RLVD", track_id=f"t{i}", now=i * 20.0)
        assert f.is_static(1758, 1021, "RLVD", now=60.0)
        assert f.is_static(1760, 1023, "RLVD", now=60.0)


class TestRecurringVehicles:
    """Measured 6-7 Sep 2026: a vehicle that comes back is not furniture.

    The simulated clips loop every 90 s, so every plate returned under a new
    track id at the same place. Without forgetting, each one was suppressed from
    its third pass on, and plate reads across the estate fell to zero for seven
    days while OCR kept working and every health check stayed green.
    """

    def test_a_plate_on_a_looping_clip_is_never_furniture(self) -> None:
        # 54 s is the fastest loop measured on the live simulated estate
        # (cam-46); a 60 s memory still suppressed plates on cam-10 at 55.8 s.
        f = StaticTextFilter()
        for loop in range(300):  # four and a half hours of a 54 s loop
            start = loop * 54.0
            assert not f.is_static(600, 500, "GJ18TR4321", now=start), f"pass {loop}"
            for s in range(5):  # in view for five seconds each pass
                f.observe(600, 500, "GJ18TR4321", track_id=f"loop-{loop}", now=start + s)

    def test_the_same_bus_on_its_timetable_is_never_furniture(self) -> None:
        f = StaticTextFilter()
        for trip in range(48):  # every 30 minutes for a day
            now = trip * 1800.0
            assert not f.is_static(600, 500, "GJ18Z1234", now=now)
            f.observe(600, 500, "GJ18Z1234", track_id=f"trip-{trip}", now=now)

    def test_an_overlay_that_keeps_being_read_stays_suppressed(self) -> None:
        """Suppressed reads never reach `observe`; the region must stay live."""
        f = StaticTextFilter()
        x, y, text = RLVD
        for i in range(4):
            f.observe(x, y, text, track_id=f"t{i}", now=i * 20.0)
        for t in range(80, 3600, 20):  # an hour, read every 20 s
            assert f.is_static(x, y, text, now=float(t)), f"leaked at {t}s"
            f.note_suppressed(x, y, text, now=float(t))

    def test_an_overlay_that_falls_silent_is_relearned_not_trusted(self) -> None:
        f = StaticTextFilter()
        x, y, text = RLVD
        for i in range(4):
            f.observe(x, y, text, track_id=f"t{i}", now=i * 20.0)
        assert f.is_static(x, y, text, now=60.0)
        assert not f.is_static(x, y, text, now=500.0)
        assert f.known_overlays == []


class TestSafety:
    def test_early_reads_are_admitted_and_kept(self) -> None:
        """Invariant 1: the first reads of a new overlay are still persisted.

        The filter only ever suppresses text it has already proved static, so a
        genuine plate is never withheld while the camera is being learned.
        """
        f = StaticTextFilter()
        assert not f.is_static(*RLVD, now=0.0)
        f.observe(*RLVD[:2], RLVD[2], track_id="t1", now=0.0)
        assert not f.is_static(*RLVD, now=0.0)

    def test_empty_text_is_ignored(self) -> None:
        f = StaticTextFilter()
        f.observe(10, 10, "   ", track_id="t1", now=0.0)
        assert not f.is_static(10, 10, "", now=0.0)

    def test_matching_is_case_and_space_insensitive(self) -> None:
        f = StaticTextFilter()
        for i in range(4):
            f.observe(100, 100, " rlvd ", track_id=f"t{i}", now=i * 20.0)
        assert f.is_static(100, 100, "RLVD", now=60.0)

    def test_the_memory_is_bounded(self) -> None:
        """A noisy feed must not grow this without limit."""
        from services.anpr.overlay import MAX_KEYS

        f = StaticTextFilter()
        for i in range(MAX_KEYS + 200):
            f.observe(i * 100, 50, f"TEXT{i}", track_id=f"t{i}", now=float(i))
        assert len(f._seen) <= MAX_KEYS


class TestReporting:
    def test_suppressed_regions_are_inspectable(self) -> None:
        """A suppression nobody can see is indistinguishable from a bug."""
        f = StaticTextFilter()
        for x, y, text in (RLVD, PTZ1, DATE):
            for i in range(4):
                f.observe(x, y, text, track_id=f"{text}-{i}", now=i * 20.0)

        found = {entry[2] for entry in f.known_overlays}
        assert found == {"RLVD", "PTZ1", "2026"}

        f.note_suppressed(*RLVD, now=60.0)
        f.note_suppressed(*RLVD, now=60.0)
        counts = {e[2]: e[3] for e in f.known_overlays}
        assert counts["RLVD"] == 2
