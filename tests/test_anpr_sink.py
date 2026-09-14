"""Turning a finished track into a row, and getting it written.

The invariant this file exists to protect is invariant 1: *every*
plate read is persisted, not just watchlist matches. The evaluator's plate
arrives after the vehicle has gone, so a filter anywhere in this path makes the
trace impossible. There is deliberately no test asserting that low-confidence
or invalid reads are dropped — because they must not be.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from services.anpr.sink import INSERT_COLUMNS, SightingRow, SightingSink, row_for
from services.anpr.tracks import CompletedTrack, Track
from services.anpr.vote import PlateRead, vote


def completed(reads: list[tuple[str, float]], **kwargs: object) -> CompletedTrack:
    track = Track(track_id="t1", camera_id="cam-uuid", first_seen=0.0, last_seen=1.0, frames=12)
    for key, value in kwargs.items():
        setattr(track, key, value)
    plate_reads = [PlateRead(raw=r, confidence=c) for r, c in reads]
    track.reads = plate_reads
    return CompletedTrack(track=track, result=vote(plate_reads), reason="left frame")


class FakeCursor:
    """Stands in for psycopg, including the RETURNING the alerting path needs.

    The sightings INSERT returns each row's database-assigned id so an alert can
    be bound to it in the same transaction, so a double that cannot fetch would
    no longer be exercising the real path.
    """

    def __init__(self, log: list) -> None:
        self.log = log
        self._returned: list[dict] = []

    def executemany(self, sql: str, params: list) -> None:
        self.log.append(("many", sql, params))

    def execute(self, sql: str, params: tuple) -> None:
        self.log.append(("one", sql, params))
        if "INSERT INTO sightings" in sql:
            from services.anpr.sink import INSERT_COLUMNS, RETURNING_COLUMNS

            width = len(INSERT_COLUMNS)
            rows = [params[i:i + width] for i in range(0, len(params), width)]
            # Tuples, not dicts. The ANPR worker's connection has no dict row
            # factory, and a double that returned mappings passed while the
            # real thing raised `tuple indices must be integers` and took down
            # every write in the estate.
            by_name = [
                dict(zip(INSERT_COLUMNS, values, strict=True)) for values in rows
            ]
            self._returned = [
                tuple(
                    1000 + n if column == "id" else row[column]
                    for column in RETURNING_COLUMNS
                )
                for n, row in enumerate(by_name)
            ]

    def fetchall(self) -> list[dict]:
        return self._returned

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *a: object) -> None:
        return None


@dataclass
class FakeConn:
    log: list = field(default_factory=list)
    fail: bool = False

    def cursor(self) -> FakeCursor:
        if self.fail:
            raise RuntimeError("database is on fire")
        return FakeCursor(self.log)

    def __enter__(self) -> FakeConn:
        return self

    def __exit__(self, *a: object) -> None:
        return None


class TestRowMapping:
    def test_a_readable_track_becomes_a_row(self) -> None:
        row = row_for(completed([("GJ01AB1234", 0.9)] * 5))
        assert row is not None
        assert row.plate_normalised == "GJ01AB1234"
        assert row.read_count == 5
        assert row.track_frames == 12

    def test_a_low_confidence_read_is_still_written(self) -> None:
        """Filtering here would lose exactly the sighting the trace needs."""
        row = row_for(completed([("GJ01AB1234", 0.2)]))
        assert row is not None
        assert row.confidence < 0.5

    def test_a_format_invalid_read_is_written_and_flagged(self) -> None:
        row = row_for(completed([("ZZZZ!!!!", 0.6), ("ZZZZ!!!!", 0.6)]))
        assert row is not None
        assert row.format_valid is False

    def test_a_track_that_read_nothing_produces_no_row(self) -> None:
        """The one case with nothing to persist: no characters at all."""
        assert row_for(completed([])) is None

    def test_the_timestamp_is_ingest_time_in_utc(self) -> None:
        """Never the burnt-in overlay: the real feeds disagree by weeks."""
        row = row_for(completed([("GJ01AB1234", 0.9)]))
        assert row is not None
        assert row.ts.tzinfo is not None
        assert abs((datetime.now(UTC) - row.ts).total_seconds()) < 5

    def test_one_ingest_time_can_be_shared_across_a_batch(self) -> None:
        """All tracks that finished on one frame carry that frame's time."""
        when = datetime(2026, 8, 18, 10, 30, tzinfo=UTC)
        rows = [row_for(completed([("GJ01AB1234", 0.9)]), when) for _ in range(3)]
        assert {r.ts for r in rows if r} == {when}

    def test_evidence_fields_survive(self) -> None:
        row = row_for(completed(
            [("GJ01AB1234", 0.9)],
            bbox=(10, 20, 110, 220), plate_bbox=(50, 180, 100, 200),
            condition="glare", slot_offset=4978.5, vehicle_class="truck",
        ))
        assert row is not None
        assert row.bbox == [10, 20, 110, 220]
        assert row.plate_bbox == [50, 180, 100, 200]
        assert row.condition == "glare"
        assert row.slot_offset == 4978.5
        assert row.vehicle_class == "truck"


class TestBatching:
    def test_rows_are_written_once_the_batch_fills(self) -> None:
        conn = FakeConn()
        sink = SightingSink(connect=lambda: conn, batch_size=3)
        rows = [row_for(completed([("GJ01AB1234", 0.9)])) for _ in range(3)]
        sink.add([r for r in rows if r])
        assert sink.written == 3
        assert conn.log[0][0] == "one"
        assert "RETURNING" in conn.log[0][1]

    def test_a_partial_batch_waits_but_not_for_long(self) -> None:
        sink = SightingSink(connect=FakeConn, batch_size=100, max_age_s=1.0)
        row = row_for(completed([("GJ01AB1234", 0.9)]))
        assert row is not None
        sink.add([row])
        assert sink.written == 0
        assert sink.due(now=sink.oldest_at) is False
        assert sink.due(now=(sink.oldest_at or 0) + 2.0) is True

    def test_an_empty_add_does_not_start_the_clock(self) -> None:
        sink = SightingSink(connect=FakeConn)
        sink.add([])
        assert sink.due() is False

    def test_a_database_failure_does_not_kill_the_pipeline(self) -> None:
        """Losing a batch is bad; losing everything after it is worse."""
        sink = SightingSink(connect=lambda: FakeConn(fail=True), batch_size=1)
        row = row_for(completed([("GJ01AB1234", 0.9)]))
        assert row is not None
        sink.add([row])
        assert sink.written == 0
        # And it keeps accepting work afterwards.
        sink.add([row])

    def test_a_flushed_batch_is_announced_for_the_alerting_path(self) -> None:
        published: list = []

        class Pub:
            def publish(self, rows: list) -> None:
                published.extend(rows)

        sink = SightingSink(connect=FakeConn, publisher=Pub(), batch_size=1)
        row = row_for(completed([("GJ01AB1234", 0.9)]))
        assert row is not None
        sink.add([row])
        assert len(published) == 1

    def test_a_failing_publisher_does_not_lose_a_written_sighting(self) -> None:
        class Pub:
            def publish(self, rows: list) -> None:
                raise RuntimeError("broker down")

        sink = SightingSink(connect=FakeConn, publisher=Pub(), batch_size=1)
        row = row_for(completed([("GJ01AB1234", 0.9)]))
        assert row is not None
        sink.add([row])
        assert sink.written == 1


class TestTheParameterOrderIsLoadBearing:
    """The class of bug that stopped every write in the estate for two hours.

    `identifying` was added to `as_params()` in the middle of the tuple while it
    was declared last in the INSERT, so `condition` was bound to `slot_offset`.
    Postgres rejected it — `invalid input syntax for type real: "day"` — which is
    the lucky case: the columns happened to have incompatible types. Two adjacent
    text columns would have silently swapped their contents and been persisted,
    and the only symptom would have been a database quietly full of wrong data.

    Nothing in the existing tests could catch it. They assert on the *dataclass*,
    which was correct throughout; the defect was in the mapping to SQL. So the
    correspondence is now pinned by name.
    """

    def test_every_insert_column_is_an_attribute_of_the_row(self) -> None:
        from services.anpr.sink import INSERT_COLUMNS, SightingRow

        fields = set(SightingRow.__dataclass_fields__)
        assert set(INSERT_COLUMNS) <= fields, "INSERT names a column the row lacks"

    def test_params_are_emitted_in_the_declared_column_order(self) -> None:
        from services.anpr.sink import INSERT_COLUMNS

        row = row_for(completed(
            [("GJ01AB1234", 0.9)],
            bbox=(1, 2, 3, 4), plate_bbox=(5, 6, 7, 8),
            condition="day", slot_offset=1234.5, vehicle_class="car",
        ))
        assert row is not None
        by_name = dict(zip(INSERT_COLUMNS, row.as_params(), strict=True))
        # Spot-check the pair that actually broke: a `condition` string must not
        # land where a REAL is expected.
        assert by_name["condition"] == "day"
        assert by_name["slot_offset"] == 1234.5
        assert by_name["identifying"] is True
        assert by_name == {c: getattr(row, c) for c in INSERT_COLUMNS}

    def test_the_placeholder_count_matches_the_column_count(self) -> None:
        from services.anpr.sink import _INSERT, INSERT_COLUMNS

        assert _INSERT.count("%s") == len(INSERT_COLUMNS)

    def test_a_short_read_is_still_written_just_flagged(self) -> None:
        """Invariant 1: `identifying` labels a read, it never withholds one."""
        from services.anpr.sink import INSERT_COLUMNS

        row = row_for(completed([("SS", 0.4)]))
        assert row is not None, "a two-character read is still a read"
        by_name = dict(zip(INSERT_COLUMNS, row.as_params(), strict=True))
        assert by_name["identifying"] is False
        assert by_name["plate_normalised"]


class TestAlertingRidesTheWrite:
    """M5's writer runs inside the sightings transaction — see the migration.

    `alerts` cannot carry a foreign key to a hypertable, so committing the alert
    with the sighting that caused it *is* the integrity constraint. These pin the
    two properties that keeps: the writer sees exactly what was written, and it
    can never take the write down with it.
    """

    def test_the_alerting_writer_sees_every_written_row(self) -> None:
        seen: list = []

        class Spy:
            def consider(self, _cur: object, rows: list) -> list:
                seen.extend(rows)
                return []

        conn = FakeConn()
        sink = SightingSink(connect=lambda: conn, batch_size=2, alerts=Spy())
        sink.add([r for r in (row_for(completed([("GJ01AB1234", 0.9)])) for _ in range(2)) if r])
        assert len(seen) == 2
        assert seen[0].plate_normalised == "GJ01AB1234"
        assert seen[0].id > 0, "the database-assigned id must reach the matcher"

    def test_rows_reach_the_matcher_as_typed_values_not_raw_columns(self) -> None:
        seen: list = []

        class Spy:
            def consider(self, _cur: object, rows: list) -> list:
                seen.extend(rows)
                return []

        conn = FakeConn()
        sink = SightingSink(connect=lambda: conn, batch_size=1, alerts=Spy())
        row = row_for(completed([("SS", 0.3)]))
        assert row is not None
        sink.add([row])
        assert seen[0].identifying is False
        assert seen[0].confidence == row.confidence


class TestRetry:
    """A dropped connection is not a bad batch.

    Measured on the live estate: `server closed the connection unexpectedly`
    lost 4 of 484 reads in a ten-minute window. Every one of them would have
    been written by asking again, and invariant 1 makes a lost read the most
    expensive failure this component has.
    """

    class Flaky:
        """Fails the first N cursor acquisitions, then behaves."""

        def __init__(self, failures: int, error: Exception) -> None:
            self.failures = failures
            self.error = error
            self.conn = FakeConn()

        def __call__(self) -> FakeConn:
            if self.failures > 0:
                self.failures -= 1
                raise self.error
            return self.conn

    def test_a_dropped_connection_is_retried_and_the_reads_survive(self) -> None:
        class OperationalError(Exception):
            pass

        connect = self.Flaky(1, OperationalError("server closed the connection"))
        sink = SightingSink(connect=connect, batch_size=1)
        row = row_for(completed([("GJ01AB1234", 0.9)]))
        assert row is not None
        sink.add([row])

        assert sink.written == 1, "the read reached the index on the second attempt"
        assert sink.retried == 1
        assert sink.failed_by_camera == {}

    def test_a_batch_the_database_refuses_is_not_retried(self) -> None:
        """It would be refused identically, and retrying stalls the writer."""
        class DataError(Exception):
            pass

        connect = self.Flaky(5, DataError("invalid input syntax for type real"))
        sink = SightingSink(connect=connect, batch_size=1)
        row = row_for(completed([("GJ01AB1234", 0.9)]))
        assert row is not None
        sink.add([row])

        assert sink.written == 0
        assert connect.failures == 4, "one attempt, not two"
        assert sum(sink.failed_by_camera.values()) == 1

    def test_a_connection_that_stays_down_gives_up_after_one_retry(self) -> None:
        class OperationalError(Exception):
            pass

        connect = self.Flaky(9, OperationalError("still down"))
        sink = SightingSink(connect=connect, batch_size=1)
        row = row_for(completed([("GJ01AB1234", 0.9)]))
        assert row is not None
        sink.add([row])

        assert connect.failures == 7, "two attempts total, then counted as lost"
        assert sum(sink.failed_by_camera.values()) == 1


class TestEmbeddingParameter:
    """pgvector's text form, and only for the column that needs it.

    The shorter version of this — "if it looks like a list of floats, render it
    as a vector" — would eventually have caught `bbox`, which is also a list.
    Keying on the column name is what stops that coincidence becoming a bug
    nobody can find.
    """

    def test_an_embedding_is_rendered_as_a_pgvector_literal(self) -> None:
        from services.anpr.sink import INSERT_COLUMNS, SightingRow, row_for

        row = row_for(completed([("GJ01AB1234", 0.9)]))
        assert row is not None
        row = SightingRow(**{**row.__dict__, "embedding": [0.5, -0.25, 0.125]})
        by_name = dict(zip(INSERT_COLUMNS, row.as_params(), strict=True))
        assert by_name["embedding"] == "[0.500000,-0.250000,0.125000]"

    def test_a_bbox_is_left_as_a_list(self) -> None:
        from services.anpr.sink import INSERT_COLUMNS

        row = row_for(completed([("GJ01AB1234", 0.9)], bbox=(1, 2, 3, 4)))
        assert row is not None
        by_name = dict(zip(INSERT_COLUMNS, row.as_params(), strict=True))
        assert by_name["bbox"] == [1, 2, 3, 4]

    def test_a_missing_embedding_stays_null(self) -> None:
        from services.anpr.sink import INSERT_COLUMNS

        row = row_for(completed([("GJ01AB1234", 0.9)]))
        assert row is not None
        by_name = dict(zip(INSERT_COLUMNS, row.as_params(), strict=True))
        assert by_name["embedding"] is None

    def test_the_statement_casts_the_embedding(self) -> None:
        from services.anpr.sink import _insert_sql

        assert _insert_sql(1).count("%s::vector") == 2, "embedding and appearance"
        assert _insert_sql(3).count("%s::vector") == 6


class TestAttributeOnlyRows:
    """Rows for vehicles that were seen and described but never read.

    This is what makes the government estate produce evidence at all: 0 of its
    30 cameras reach ANPR grade, so `result` is None for essentially every
    vehicle they see, and before M15 that meant thirty cameras watching
    thousands of vehicles go past and writing nothing.
    """

    @staticmethod
    def unread(**kwargs: object) -> CompletedTrack:
        track = Track(
            track_id="t9", camera_id="cam-uuid", first_seen=0.0, last_seen=3.0, frames=12
        )
        for key, value in kwargs.items():
            setattr(track, key, value)
        return CompletedTrack(track=track, result=None, reason="left frame")

    def test_a_described_vehicle_produces_a_row(self) -> None:
        row = row_for(self.unread(vehicle_colour="silver", vehicle_class="car"))
        assert row is not None
        assert row.vehicle_colour == "silver"
        assert row.vehicle_class == "car"

    def test_the_plate_is_empty_rather_than_null(self) -> None:
        """`plate_normalised` has been NOT NULL since 001 and every query in
        the platform depends on it. An empty string is the compatible way to
        say "no plate"."""
        row = row_for(self.unread(vehicle_colour="silver"))
        assert row is not None
        assert (row.plate_raw, row.plate_normalised) == ("", "")
        assert row.confidence == 0.0
        assert row.read_count == 0

    def test_it_is_marked_non_identifying(self) -> None:
        """Which is the flag M5 already built: fuzzy search and the alert
        matcher both filter on it, so an empty plate cannot reach either by
        construction rather than by a new rule."""
        row = row_for(self.unread(vehicle_colour="silver"))
        assert row is not None
        assert row.identifying is False
        assert row.format_valid is False

    def test_a_vehicle_with_no_description_writes_nothing(self) -> None:
        """A row with neither a plate nor a description is indistinguishable
        from every other vehicle on the road."""
        assert row_for(self.unread()) is None

    def test_a_glimpse_is_not_enough(self) -> None:
        """A vehicle the tracker held for two frames is one it is not yet sure
        about. Plate reads are held to no such bar — invariant 1 is
        unconditional — but a row carrying no plate has to earn its place."""
        assert row_for(self.unread(vehicle_colour="silver", frames=2)) is None
        assert row_for(self.unread(vehicle_colour="silver", frames=12)) is not None

    def test_a_read_vehicle_still_carries_its_description(self) -> None:
        """The description is not an alternative to a plate. A camera that
        manages both stores both, and the report prints both."""
        row = row_for(completed(
            [("GJ01AB1234", 0.9)], vehicle_colour="white", colour_confidence=0.82,
        ))
        assert row is not None
        assert row.plate_normalised == "GJ01AB1234"
        assert (row.vehicle_colour, row.colour_confidence) == ("white", 0.82)

    def test_the_returning_clause_selects_every_column_it_unpacks(self) -> None:
        """The bug this pins cost the first live run after the columns landed.

        `RETURNING_COLUMNS` and the SQL clause used to be two hand-written
        declarations of one order. Adding two columns to the tuple alone raised
        `KeyError: 'vehicle_colour'` against the live database on the first row
        written — and the unit tests passed, because the test double builds its
        rows from the tuple rather than from the SQL. The clause is derived
        now; this is what keeps it derived.
        """
        from services.anpr.sink import _RETURNING, RETURNING_COLUMNS

        for column in RETURNING_COLUMNS:
            assert column in _RETURNING, f"{column} is unpacked but never selected"

    def test_the_new_columns_are_in_the_insert_contract(self) -> None:
        """The column order is load-bearing and silently so — appending
        `identifying` to this tuple once bound `condition` to `slot_offset` and
        took every write in the estate down for two hours."""
        assert "vehicle_colour" in INSERT_COLUMNS
        assert "colour_confidence" in INSERT_COLUMNS
        for column in INSERT_COLUMNS:
            assert hasattr(SightingRow(
                ts=datetime.now(UTC), camera_id="c", plate_raw="", plate_normalised="",
                confidence=0.0, format_valid=False, track_id=None, read_count=0,
                track_frames=0, vehicle_class=None, bbox=None, plate_bbox=None,
                condition=None, slot_offset=None,
            ), column)
