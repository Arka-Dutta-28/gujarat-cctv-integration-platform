"""Raising alerts on the write path, and the suppression that keeps them usable.

Two things are being protected. First, an alert must never point at a sighting
that is not in the index — `alerts` cannot carry a foreign key to a hypertable,
so the writer's use of the sighting-writing transaction *is* the integrity
constraint. Second, an alert console that repeats itself is one an operator
stops reading, and the pipeline cuts a track every 30 seconds by design.
"""

from __future__ import annotations

from datetime import UTC, datetime

from services.alerting import writer as writer_module
from services.alerting.tiers import WatchlistEntry
from services.alerting.watchlist import WatchlistCache
from services.alerting.writer import AlertWriter, WrittenSighting

WANTED = WatchlistEntry(
    id="wl-1", plate="GJ18TR4321", plate_normalised="GJ18TR4321",
    category="stolen", severity=5,
)


class FakeCursor:
    def __init__(self) -> None:
        self.inserts: list[tuple] = []
        self.fail = False

    def execute(self, sql: str, params: tuple) -> None:
        if self.fail:
            raise RuntimeError("database is on fire")
        self.inserts.append(params)


def cache(entries: list[WatchlistEntry]) -> WatchlistCache:
    return WatchlistCache(connect=None, loader=lambda _c: entries)


def sighting(plate: str = "GJ18TR4321", *, camera: str = "cam-a", conf: float = 0.95,
             sid: int = 1, identifying: bool = True) -> WrittenSighting:
    return WrittenSighting(
        id=sid, ts=datetime.now(UTC), camera_id=camera,
        plate_normalised=plate, confidence=conf, identifying=identifying,
    )


class TestRaising:
    def test_a_matching_sighting_raises_one_alert(self) -> None:
        writer, cur = AlertWriter(watchlist=cache([WANTED])), FakeCursor()
        raised = writer.consider(cur, [sighting()], now=0.0)
        assert len(raised) == 1
        assert len(cur.inserts) == 1
        sighting_id, _ts, watchlist_id, camera_id, tier, priority = cur.inserts[0]
        assert (sighting_id, watchlist_id, camera_id) == (1, "wl-1", "cam-a")
        assert (tier, priority) == ("confirmed", 5)

    def test_a_non_matching_sighting_raises_nothing(self) -> None:
        writer, cur = AlertWriter(watchlist=cache([WANTED])), FakeCursor()
        assert writer.consider(cur, [sighting("MH12XY0000")], now=0.0) == []
        assert cur.inserts == []

    def test_an_empty_watchlist_short_circuits(self) -> None:
        writer, cur = AlertWriter(watchlist=cache([])), FakeCursor()
        assert writer.consider(cur, [sighting()], now=0.0) == []

    def test_a_read_too_short_to_identify_raises_nothing(self) -> None:
        writer, cur = AlertWriter(watchlist=cache([WANTED])), FakeCursor()
        assert writer.consider(cur, [sighting("GJ18", identifying=False)], now=0.0) == []


class TestSuppression:
    def test_the_same_vehicle_at_the_same_camera_alerts_once(self) -> None:
        """A parked car yields a sighting every 30 s — one alert, not many."""
        writer, cur = AlertWriter(watchlist=cache([WANTED])), FakeCursor()
        for t in (0.0, 30.0, 60.0, 90.0):
            writer.consider(cur, [sighting(sid=int(t))], now=t)
        assert len(cur.inserts) == 1
        assert writer.suppressed == 3

    def test_the_same_vehicle_at_the_next_camera_alerts_immediately(self) -> None:
        """Movement is the thing worth knowing; suppression must not hide it."""
        writer, cur = AlertWriter(watchlist=cache([WANTED])), FakeCursor()
        writer.consider(cur, [sighting(camera="cam-a", sid=1)], now=0.0)
        writer.consider(cur, [sighting(camera="cam-b", sid=2)], now=5.0)
        assert len(cur.inserts) == 2

    def test_suppression_expires(self) -> None:
        writer, cur = AlertWriter(watchlist=cache([WANTED]), dedup_window_s=60), FakeCursor()
        writer.consider(cur, [sighting(sid=1)], now=0.0)
        writer.consider(cur, [sighting(sid=2)], now=61.0)
        assert len(cur.inserts) == 2

    def test_a_possible_match_is_suppressed_for_longer(self) -> None:
        """Several different plates can land two characters from one entry."""
        writer, cur = AlertWriter(watchlist=cache([WANTED]), dedup_window_s=10), FakeCursor()
        writer.consider(cur, [sighting("GJ18TR4300", sid=1)], now=0.0)
        writer.consider(cur, [sighting("GJ18TR4300", sid=2)], now=15.0)
        assert len(cur.inserts) == 1, "still inside the widened possible window"
        writer.consider(cur, [sighting("GJ18TR4300", sid=3)], now=31.0)
        assert len(cur.inserts) == 2


class TestFailureIsContained:
    def test_a_failing_alert_insert_never_propagates(self) -> None:
        """It would roll back the sightings themselves.

        The write path is committing an alert and a sighting together on purpose.
        That is what keeps the alert bound to a real row — but it also means an
        exception escaping here would trade an alerting failure for a breach of
        invariant 1, which is the more serious of the two by far.
        """
        writer, cur = AlertWriter(watchlist=cache([WANTED])), FakeCursor()
        cur.fail = True
        assert writer.consider(cur, [sighting()], now=0.0) == []
        assert writer.raised == 0

    def test_a_failed_insert_is_not_remembered_as_suppressing_later_ones(self) -> None:
        writer, cur = AlertWriter(watchlist=cache([WANTED])), FakeCursor()
        cur.fail = True
        writer.consider(cur, [sighting(sid=1)], now=0.0)
        cur.fail = False
        writer.consider(cur, [sighting(sid=2)], now=1.0)
        assert len(cur.inserts) == 1, "the retry must not be suppressed by the failure"


class TestWatchlistCache:
    def test_it_refreshes_on_the_timer(self) -> None:
        loads = []

        def loader(_c: object) -> list[WatchlistEntry]:
            loads.append(1)
            return [WANTED]

        c = WatchlistCache(connect=None, refresh_s=2.0, loader=loader)
        c.entries(now=0.0)
        c.entries(now=1.0)
        assert len(loads) == 1
        c.entries(now=2.0)
        assert len(loads) == 2

    def test_a_failed_refresh_keeps_the_last_good_copy(self) -> None:
        """An empty list would silently stop all alerting during a blip."""
        state = {"fail": False}

        def loader(_c: object) -> list[WatchlistEntry]:
            if state["fail"]:
                raise RuntimeError("database is on fire")
            return [WANTED]

        c = WatchlistCache(connect=None, refresh_s=1.0, loader=loader)
        assert c.entries(now=0.0) == [WANTED]
        state["fail"] = True
        assert c.entries(now=5.0) == [WANTED]
        assert c.failures == 1

    def test_a_failing_refresh_does_not_retry_on_every_sighting(self) -> None:
        """This is the pipeline's hot path; a retry storm would stall decoding."""
        attempts = []

        def loader(_c: object) -> list[WatchlistEntry]:
            attempts.append(1)
            raise RuntimeError("down")

        c = WatchlistCache(connect=None, refresh_s=2.0, loader=loader)
        for t in (0.0, 0.1, 0.5, 1.9):
            c.entries(now=t)
        assert len(attempts) == 1


DESCRIBED = WatchlistEntry(
    id="wl-2", plate="GJ05UV9972", plate_normalised="GJ05UV9972",
    category="stolen", severity=5, vehicle_colour="white", vehicle_class="truck",
)


def unread(*, camera: str = "cam-b", sid: int = 9, colour: str | None = "white",
           klass: str | None = "truck") -> WrittenSighting:
    """A sighting from a camera that saw a vehicle and could not read it.

    The empty plate is what the sink writes: `plate_normalised` has been NOT
    NULL since 001, so an attribute-only row stores '' rather than relaxing a
    constraint every query in the platform depends on.
    """
    return WrittenSighting(
        id=sid, ts=datetime.now(UTC), camera_id=camera, plate_normalised="",
        confidence=0.0, identifying=False,
        vehicle_colour=colour, vehicle_class=klass,
    )


class TestAppearanceCorroboration:
    """The `attribute` tier on the write path.

    The property being protected is that a description can only ever *extend*
    a trace a plate started. Every test here is a way that could fail open.
    """

    def test_an_unread_vehicle_alone_raises_nothing(self) -> None:
        writer, cur = AlertWriter(watchlist=cache([DESCRIBED])), FakeCursor()
        assert writer.consider(cur, [unread()], now=0.0) == []
        assert cur.inserts == []

    def test_a_plate_match_licenses_an_appearance_match_after_it(self) -> None:
        writer, cur = AlertWriter(watchlist=cache([DESCRIBED])), FakeCursor()
        writer.consider(cur, [sighting("GJ05UV9972", camera="cam-a", sid=1)], now=0.0)
        raised = writer.consider(cur, [unread(sid=9)], now=30.0)
        assert [r.match.tier for r in raised] == ["attribute"]
        assert cur.inserts[-1][4] == "attribute"

    def test_the_appearance_alert_is_demoted_below_the_plate_alert(self) -> None:
        writer, cur = AlertWriter(watchlist=cache([DESCRIBED])), FakeCursor()
        writer.consider(cur, [sighting("GJ05UV9972", camera="cam-a", sid=1)], now=0.0)
        writer.consider(cur, [unread(sid=9)], now=30.0)
        plate_priority, attribute_priority = cur.inserts[0][5], cur.inserts[1][5]
        assert attribute_priority < plate_priority

    def test_corroboration_expires(self) -> None:
        """Beyond the window the population of same-coloured vehicles grows
        faster than the evidence does, so the tier goes quiet."""
        writer, cur = AlertWriter(watchlist=cache([DESCRIBED])), FakeCursor()
        writer.consider(cur, [sighting("GJ05UV9972", camera="cam-a", sid=1)], now=0.0)
        later = writer_module.CORROBORATION_WINDOW_S + 1.0
        assert writer.consider(cur, [unread(sid=9)], now=later) == []

    def test_an_appearance_match_does_not_corroborate_the_next_one(self) -> None:
        """Otherwise one plate read licenses an unbounded chain of colour
        alerts, each corroborated by the last — a safeguard as rubber stamp."""
        writer, cur = AlertWriter(watchlist=cache([DESCRIBED])), FakeCursor()
        writer.consider(cur, [sighting("GJ05UV9972", camera="cam-a", sid=1)], now=0.0)
        writer.consider(cur, [unread(sid=9, camera="cam-b")], now=30.0)
        beyond = writer_module.CORROBORATION_WINDOW_S + 1.0
        assert writer.consider(cur, [unread(sid=10, camera="cam-c")], now=beyond) == []

    def test_a_different_description_does_not_match(self) -> None:
        writer, cur = AlertWriter(watchlist=cache([DESCRIBED])), FakeCursor()
        writer.consider(cur, [sighting("GJ05UV9972", camera="cam-a", sid=1)], now=0.0)
        assert writer.consider(cur, [unread(sid=9, colour="red")], now=30.0) == []

    def test_the_detector_label_is_mapped_before_matching(self) -> None:
        """`sightings.vehicle_class` stores COCO's label; the watchlist is
        written in human words. A two-wheeler entry must match a `motorcycle`
        sighting, and this is the only place the two vocabularies meet."""
        entry = WatchlistEntry(
            id="wl-3", plate="GJ07ZZ1234", plate_normalised="GJ07ZZ1234",
            category="stolen", severity=4,
            vehicle_colour="black", vehicle_class="two-wheeler",
        )
        writer, cur = AlertWriter(watchlist=cache([entry])), FakeCursor()
        writer.consider(cur, [sighting("GJ07ZZ1234", camera="cam-a", sid=1)], now=0.0)
        raised = writer.consider(
            cur, [unread(sid=9, colour="black", klass="motorcycle")], now=30.0
        )
        assert [r.match.tier for r in raised] == ["attribute"]

    def test_an_unread_vehicle_never_reaches_the_plate_matcher(self) -> None:
        """An empty plate is within edit distance 2 of nothing, but the guard
        is structural rather than incidental: `read_a_plate` is what routes it,
        so a future change to the distance rules cannot let '' through."""
        assert not unread().read_a_plate
        assert sighting().read_a_plate
