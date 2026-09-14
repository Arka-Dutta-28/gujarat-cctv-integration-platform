"""Persisting what the pipeline produced.

Invariant 1, restated because everything here exists to serve it: *every plate
read goes into `sightings` unconditionally.* The designated registration number
is handed over during the live evaluation, by which time the vehicle has already
driven past. A sink that filtered to watchlist hits would make the trace
impossible, so there is no filter in this module — not on confidence, not on
format validity, not on whether the plate is wanted.

Writes are batched because a per-sighting round trip would put the database in
the pipeline's latency path, and time spent in `INSERT` is time not spent
decoding. The batch is small and time-bounded: a sighting that sits in a buffer
is a sighting the alerting path has not seen yet.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from services.anpr.metrics import MetricsCollector
from services.anpr.tracks import CompletedTrack

log = logging.getLogger("anpr.sink")

__all__ = ["SightingRow", "row_for", "SightingSink", "BATCH_SIZE", "BATCH_MAX_AGE_S"]

#: Rows per INSERT. Large enough that the round trip disappears, small enough
#: that a batch is never worth much latency.
BATCH_SIZE = 25

#: A sighting must not wait longer than this to be written, however quiet the
#: camera is. Alerting reads what has been written.
BATCH_MAX_AGE_S = 1.0


#: The single declaration of column order, so the SQL and the parameter tuple
#: cannot drift apart. Every name here must be an attribute of `SightingRow`.
INSERT_COLUMNS: tuple[str, ...] = (
    "ts", "camera_id", "plate_raw", "plate_normalised", "confidence",
    "format_valid", "track_id", "read_count", "track_frames", "vehicle_class",
    "bbox", "plate_bbox", "condition", "slot_offset", "identifying", "crop_path",
    "embedding", "vehicle_colour", "colour_confidence", "appearance",
)

#: The alerting path needs the database-assigned id of each row it just wrote,
#: so the INSERT returns the sighting rather than being fire-and-forget. Returned
#: rather than zipped against the input by position: a multi-row VALUES does
#: return rows in insertion order in practice, but nothing in the standard
#: promises it, and an alert bound to the wrong sighting is evidence pointing at
#: the wrong vehicle.
#: Read positionally, because the ANPR worker's connection has no dict row
#: factory — unlike the API's pool, which does. Indexing these by name worked in
#: the unit tests and raised `tuple indices must be integers` against the live
#: database, taking every write in the estate down with it for four minutes.
#: One order, declared once, used by both the SQL and the unpacking.
RETURNING_COLUMNS: tuple[str, ...] = (
    "id", "ts", "camera_id", "plate_normalised", "confidence", "identifying",
    "vehicle_colour", "vehicle_class",
)
#: Columns the RETURNING clause has to select differently from how it names
#: them. `camera_id` is a uuid and the alerting path compares it as text.
_RETURNING_EXPR = {"camera_id": "camera_id::text AS camera_id"}

#: Derived, never written out by hand. This clause and the unpacking above were
#: two separate declarations of the same order until 6 Sep 2026, when adding
#: two columns to `RETURNING_COLUMNS` alone raised `KeyError: 'vehicle_colour'`
#: against the live database on the first row written — while the unit tests
#: passed, because the test double built its rows from the tuple rather than
#: from the SQL. The lesson is the same one the block above records: there is
#: one order, it is declared once, and everything else is computed from it.
_RETURNING = ", ".join(
    _RETURNING_EXPR.get(column, column) for column in RETURNING_COLUMNS
)


#: Columns whose parameter needs an explicit cast. `vector` has no psycopg
#: adapter here, and adding the `pgvector` package for one column would be a
#: dependency on a link that has truncated large downloads repeatedly — the
#: value is sent as its text form and cast in the statement instead.
CASTS = {"embedding": "::vector", "appearance": "::vector"}


def _insert_sql(rows: int) -> str:
    """One multi-row INSERT ... RETURNING for a whole batch."""
    tuple_sql = "(" + ", ".join(
        f"%s{CASTS.get(column, '')}" for column in INSERT_COLUMNS
    ) + ")"
    return (
        f"INSERT INTO sightings ({', '.join(INSERT_COLUMNS)}) VALUES "
        + ", ".join([tuple_sql] * rows)
        + f" RETURNING {_RETURNING}"
    )


#: Single-row form, kept for the tests that assert the column contract.
_INSERT = _insert_sql(1)


@dataclass
class SightingRow:
    """One row of `sightings`, ready to insert."""

    ts: datetime
    camera_id: str
    plate_raw: str
    plate_normalised: str
    confidence: float
    format_valid: bool
    track_id: str | None
    read_count: int
    track_frames: int
    vehicle_class: str | None
    bbox: list[int] | None
    plate_bbox: list[int] | None
    condition: str | None
    slot_offset: float | None
    #: False when the read is too short to narrow a search. Still persisted —
    #: see db/migrations/007. The flag governs what a read may *drive*.
    identifying: bool = True
    #: Where the evidence JPEG landed, relative to the crop volume. None when
    #: the crop could not be written — the sighting is still a sighting.
    crop_path: str | None = None
    #: Appearance descriptor for re-identification. None is normal: every read
    #: from before M9, and every one whose crop could not be encoded, has none.
    embedding: list[float] | None = None
    #: What a person would call this vehicle. None means the platform declined
    #: to name a colour, never "no colour" — see services/anpr/attributes.py.
    vehicle_colour: str | None = None
    colour_confidence: float | None = None
    #: Learned appearance vector (`reid.embed_many`), used to link sightings of
    #: one vehicle across cameras. None when the model is not in this image.
    appearance: list[float] | None = None

    def as_params(self) -> tuple[Any, ...]:
        """Values in _INSERT's column order.

        The order is load-bearing and silently so. Appending `identifying` here while it
        was declared last in the INSERT bound `condition` to `slot_offset`, and every
        write in the estate failed for two hours on "invalid input syntax for type real:
        day". Postgres caught it only because the types happened to disagree; two
        adjacent text columns would have swapped their contents and been persisted.
        test_anpr_sink pins the correspondence by name so this cannot recur.
        """
        return tuple(
            _as_param(column, getattr(self, column)) for column in INSERT_COLUMNS
        )


#: Below this many characters a read cannot narrow a search: `GJ18` locates a
#: district and is worth keeping in the fuzzy path, `SS` and `4` are not. A
#: proxy, deliberately — see db/migrations/007_identifying_reads.sql.
MIN_IDENTIFYING_CHARS = 4


def _store_crop(track: Any, ts: datetime | None) -> str | None:
    """Write the track's evidence JPEG, if it has one.

    Deliberately at row-building time rather than during tracking: only a track
    that produced a plate, or an appearance vector (so a vehicle-id link can be
    checked by eye), gets a file.
    """
    data = getattr(track, "crop_jpeg", None)
    if not data:
        return None
    from services.anpr import crops

    return crops.save(data, track.camera_id, track.track_id, ts)


#: Frames a track must have lasted before an *attribute-only* row is worth
#: writing. A vehicle seen for two frames is a vehicle the tracker is not yet
#: sure about, and a description taken from it is a description of a maybe.
#: Plate reads are held to no such bar — invariant 1 is unconditional — but a
#: row carrying no plate has to earn its place, because these are written for
#: every vehicle on the road rather than for the handful that were read.
MIN_ATTRIBUTE_FRAMES = 5


def _attribute_row(track: Any, ts: datetime | None) -> SightingRow | None:
    """A row for a vehicle that was described but never read.

    This is the row that makes the government estate produce evidence. 0 of
    those 30 cameras reach ANPR grade, so `completed.result` is None for
    essentially every vehicle they see; before this, that meant thirty cameras
    writing nothing at all while watching thousands of vehicles go past.

    The plate columns are empty strings rather than NULL — the schema has
    declared them NOT NULL since 001 and relaxing that would touch every query
    in the platform — and `identifying` is False, which is the flag the fuzzy
    search and the alert matcher already use to decide what a read is allowed
    to *drive*. An empty plate therefore cannot reach either by construction,
    using the mechanism M5 already built rather than a new one.

    Deliberately not a partial trace record: this row says "a silver car passed
    camera 21 at 14:32", which is exactly what the camera knows, and nothing
    about which silver car.
    """
    if track.frames < MIN_ATTRIBUTE_FRAMES:
        return None
    if not track.vehicle_colour and not track.vehicle_class:
        return None
    return SightingRow(
        ts=ts or datetime.now(UTC),
        camera_id=track.camera_id,
        plate_raw="",
        plate_normalised="",
        confidence=0.0,
        format_valid=False,
        track_id=track.track_id,
        read_count=0,
        track_frames=track.frames,
        vehicle_class=track.vehicle_class,
        bbox=list(track.bbox) if track.bbox else None,
        plate_bbox=None,
        condition=track.condition,
        slot_offset=track.slot_offset,
        identifying=False,
        crop_path=_store_crop(track, ts) if getattr(track, "appearance", None) else None,
        embedding=getattr(track, "embedding", None),
        vehicle_colour=track.vehicle_colour,
        colour_confidence=track.colour_confidence or None,
        appearance=getattr(track, "appearance", None),
    )


def row_for(completed: CompletedTrack, ts: datetime | None = None) -> SightingRow | None:
    """Turn a finished track into a row, or None if there is nothing to say.

    ts is ingest time, never the burnt-in overlay clock: the real feeds disagree
    with each other by weeks, and a journey reconstructed from their overlays would
    be nonsense (docs/field-observations.md section 3).

    A track that read no plate is no longer discarded outright. If it was seen long
    enough to describe, it produces an attribute-only row; if it was not, it still
    produces nothing, because a row with neither a plate nor a description is
    indistinguishable from every other vehicle on the road.
    """
    track = completed.track
    result = completed.result
    if result is None:
        return _attribute_row(track, ts)

    return SightingRow(
        ts=ts or datetime.now(UTC),
        camera_id=track.camera_id,
        plate_raw=result.plate_raw,
        plate_normalised=result.plate_normalised,
        confidence=result.confidence,
        format_valid=result.format_valid,
        track_id=track.track_id,
        read_count=result.read_count,
        track_frames=track.frames,
        vehicle_class=track.vehicle_class,
        bbox=list(track.bbox) if track.bbox else None,
        plate_bbox=list(track.plate_bbox) if track.plate_bbox else None,
        condition=track.condition,
        slot_offset=track.slot_offset,
        identifying=len(result.plate_normalised.strip()) >= MIN_IDENTIFYING_CHARS,
        crop_path=_store_crop(track, ts),
        embedding=getattr(track, "embedding", None),
        vehicle_colour=getattr(track, "vehicle_colour", None),
        colour_confidence=getattr(track, "colour_confidence", None) or None,
        appearance=getattr(track, "appearance", None),
    )



_STAGE_STATS = """
INSERT INTO anpr_stage_stats (ts, camera_id, stage, samples, p50_ms, p95_ms, max_ms)
VALUES (date_trunc('minute', now()), %s, %s, %s, %s, %s, %s)
ON CONFLICT (ts, camera_id, stage) DO UPDATE SET
    samples = anpr_stage_stats.samples + EXCLUDED.samples,
    p50_ms  = EXCLUDED.p50_ms,
    p95_ms  = GREATEST(anpr_stage_stats.p95_ms, EXCLUDED.p95_ms),
    max_ms  = GREATEST(anpr_stage_stats.max_ms, EXCLUDED.max_ms)
"""

#: Counters kept in `counters` JSONB rather than as typed columns. Each is the
#: evidence for a design decision — the per-track read budget, shedding rather
#: than queueing under load, the relay fallback, the composite tracker's switch
#: — and a design justified by a counter nobody can read is justified by
#: assertion. They are secondary to the five typed columns, which are what
#: `/api/performance` aggregates on every request.
SECONDARY_COUNTERS = (
    "ocr_skipped_settled",
    "ocr_shed",
    "via_relay",
    "motion_fallback_windows",
    "overlay_suppressed",
    # Reads the pipeline produced and the database refused. Distinct from every
    # other counter here: the rest describe work deliberately not done, this one
    # is data lost. It exists because a parameter-order bug failed every write in
    # the estate for two hours while `sightings_written` — counted when a row was
    # *built* — kept climbing, so the performance page showed a healthy pipeline
    # writing nothing. A silent breach of invariant 1 is the worst failure this
    # platform has, and it must be visible in the same place the throughput is.
    "sightings_write_failed",
)

_THROUGHPUT = """
INSERT INTO anpr_throughput (
    ts, camera_id, frames_decoded, frames_analysed, vehicles_tracked,
    plate_reads, sightings_written, decode_fps, counters
) VALUES (date_trunc('minute', now()), %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (ts, camera_id) DO UPDATE SET
    frames_decoded    = anpr_throughput.frames_decoded + EXCLUDED.frames_decoded,
    frames_analysed   = anpr_throughput.frames_analysed + EXCLUDED.frames_analysed,
    vehicles_tracked  = anpr_throughput.vehicles_tracked + EXCLUDED.vehicles_tracked,
    plate_reads       = anpr_throughput.plate_reads + EXCLUDED.plate_reads,
    sightings_written = anpr_throughput.sightings_written + EXCLUDED.sightings_written,
    decode_fps        = EXCLUDED.decode_fps,
    -- Additive, like the typed columns beside it: a rollup adds to the minute
    -- rather than replacing it, so two workers writing the same camera-minute
    -- during a handover do not lose one another's counts.
    counters          = (
        SELECT jsonb_object_agg(key, value)
        FROM (
            SELECT key, sum(value::bigint) AS value
            FROM (
                SELECT key, value FROM jsonb_each_text(anpr_throughput.counters)
                UNION ALL
                SELECT key, value FROM jsonb_each_text(EXCLUDED.counters)
            ) merged
            GROUP BY key
        ) summed
    )
"""


def _as_param(column: str, value: Any) -> Any:
    """Render a value the way the database expects it.

    Keyed on the column rather than on the value's type. Sniffing for "a list of
    floats" would have been shorter and would eventually have caught `bbox`,
    which is also a list — the kind of coincidence that produces a bug nobody
    can find later.

    Only the embedding needs this: pgvector's text form is `[1,2,3]`, which a
    Python list does not produce and which psycopg would send as an array
    literal that will not cast.
    """
    if column in CASTS and value is not None:
        return "[" + ",".join(f"{x:.6f}" for x in value) + "]"
    return value


#: Errors worth trying again. Connection-level faults only: a batch the
#: database refuses on its merits — a bad value, a constraint — fails
#: identically on a second attempt, and retrying it would turn one lost batch
#: into a stalled writer.
RETRYABLE = ("OperationalError", "InterfaceError", "AdminShutdown", "ConnectionException")


def _is_retryable(error: BaseException) -> bool:
    return type(error).__name__ in RETRYABLE


def _written(row: Any) -> Any:
    """Adapt one returned sightings row for the alerting matcher.

    Accepts either a tuple or a mapping: the worker's plain connection yields
    tuples and the API's pool yields dicts, and this module is used from both.
    """
    from services.alerting.writer import WrittenSighting

    values = (
        [row[name] for name in RETURNING_COLUMNS]
        if hasattr(row, "keys")
        else list(row)
    )
    (
        id_, ts, camera_id, plate_normalised, confidence, identifying,
        vehicle_colour, vehicle_class,
    ) = values
    return WrittenSighting(
        id=id_, ts=ts, camera_id=camera_id, plate_normalised=plate_normalised,
        confidence=confidence, identifying=identifying,
        vehicle_colour=vehicle_colour, vehicle_class=vehicle_class,
    )


class Publisher(Protocol):
    """Where a written sighting is announced for the live alerting path.

    Kept as a seam rather than a call into Redpanda, because alerting (M5) is a
    *separate code path* from retrospective trace and must stay that way — but
    it reads the same sightings, and this is where it hears about them.
    """

    def publish(self, rows: list[SightingRow]) -> None: ...


@dataclass
class SightingSink:
    """Batched writer. One per worker process, shared across its cameras."""

    connect: Any
    publisher: Publisher | None = None
    #: The live alerting path (M5). None leaves the sink write-only, which is
    #: what the pipeline's own tests use.
    alerts: Any = None
    batch_size: int = BATCH_SIZE
    max_age_s: float = BATCH_MAX_AGE_S
    pending: list[SightingRow] = field(default_factory=list)
    oldest_at: float | None = None
    written: int = 0
    #: Batches recovered by the single retry. Reported, because a retry that
    #: silently fixes a recurring fault hides a database that needs attention.
    retried: int = 0
    #: Alerts raised since the last rollup, for the counters.
    _raised: list = field(default_factory=list)
    #: Rows the database refused, per camera, awaiting the next metrics rollup.
    #: Attributed per camera rather than kept as one total so the performance
    #: page can say *which* cameras lost reads.
    failed_by_camera: dict[str, int] = field(default_factory=dict)
    #: Give each written sighting a vehicle id (`services/anpr/linking.py`).
    #: Off by default so the pipeline's own tests stay write-only.
    link: bool = False

    def add(self, rows: list[SightingRow]) -> None:
        if not rows:
            return
        if self.oldest_at is None:
            self.oldest_at = time.monotonic()
        self.pending.extend(rows)
        if len(self.pending) >= self.batch_size:
            self.flush()

    def due(self, now: float | None = None) -> bool:
        if not self.pending or self.oldest_at is None:
            return False
        return (now or time.monotonic()) - self.oldest_at >= self.max_age_s

    def flush(self) -> int:
        if not self.pending:
            return 0
        batch, self.pending, self.oldest_at = self.pending, [], None
        written: list = []
        try:
            written = self._write(batch)
        except Exception as first:  # noqa: BLE001 - decided by `_is_retryable`
            # A dropped connection is not a bad batch. Measured on this estate:
            # `server closed the connection unexpectedly` lost 4 of 484 reads in
            # a ten-minute window, and every one of them would have been written
            # by asking again — the sink opens a connection per flush, so the
            # retry gets a fresh one. Once only: a batch the database actively
            # refuses will be refused identically the second time, and retrying
            # it forever would block the writer instead of losing one batch.
            # Losing a batch is bad, but a pipeline that dies on a database
            # hiccup loses everything after it too: log it, count it against the
            # cameras whose reads were lost, and carry on.
            #
            # Logged on *every* path, including the one that gives up
            # immediately. An earlier version of this refactor logged only after
            # a failed retry, and a data error was then counted silently — 248
            # reads lost with nothing in the log to say why, which is precisely
            # the failure `write_health` exists to make impossible.
            if not _is_retryable(first):
                log.exception("failed to write %d sightings", len(batch))
                return self._lost(batch)
            log.warning(
                "write failed (%s); retrying %d sightings once",
                type(first).__name__, len(batch),
            )
            try:
                written = self._write(batch)
                self.retried += 1
            except Exception:
                log.exception("retry failed; losing %d sightings", len(batch))
                return self._lost(batch)

        self.written += len(batch)
        self._link(written)
        if self.publisher is not None:
            try:
                self.publisher.publish(batch)
            except Exception:  # noqa: BLE001 - the sighting is already durable
                log.exception("failed to publish %d sightings", len(batch))
        return len(batch)

    def _link(self, written: list) -> None:
        """Vehicle ids for rows already committed. A failure here loses ids, never reads."""
        if not self.link or not written:
            return
        from services.anpr import linking

        try:
            with self.connect() as conn, conn.cursor() as cur:
                linking.link_batch(cur, [(w.id, w.ts) for w in map(_written, written)])
        except Exception:  # noqa: BLE001 - the sightings are already durable
            log.exception("could not link %d sightings to vehicle ids", len(written))

    def _write(self, batch: list[SightingRow]) -> list:
        """One attempt: the sightings and any alerts they raise, in one commit."""
        with self.connect() as conn, conn.cursor() as cur:
            params = [value for row in batch for value in row.as_params()]
            cur.execute(_insert_sql(len(batch)), params)
            written = cur.fetchall()
            # In the same transaction, deliberately: `alerts` cannot carry a
            # foreign key to a hypertable, so committing the two together is
            # what stops an alert pointing at a sighting that is not there.
            if self.alerts is not None:
                self._raised.extend(
                    self.alerts.consider(cur, [_written(r) for r in written])
                )
            return written

    def _lost(self, batch: list[SightingRow]) -> int:
        """Account for reads that did not reach the index.

        Counted per camera so the performance page can say *which* cameras lost
        reads. The log line alone was not enough — it scrolled past under ffmpeg
        noise for two hours while every write in the estate was failing.
        """
        for row in batch:
            self.failed_by_camera[row.camera_id] = (
                self.failed_by_camera.get(row.camera_id, 0) + 1
            )
        return 0

    def record_tamper(self, camera_id: str, verdict: Any) -> None:
        """Write one tamper event. Written immediately, not batched.

        These are rare — a handful a day across an estate — and each one is
        something an operator should see now rather than at the next rollup.
        Failure is logged and swallowed: a camera that may have been interfered
        with is not a reason to stop reading the ones that have not.
        """
        try:
            with self.connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO camera_tamper_events (camera_id, kind, detail, value)"
                    " VALUES (%s, %s, %s, %s)",
                    (camera_id, verdict.kind, verdict.detail, float(verdict.value)),
                )
        except Exception:  # noqa: BLE001
            log.exception("failed to record tamper event for %s", camera_id)

    # --- instrumentation ---

    def write_metrics(self, metrics: MetricsCollector, decode_fps: float | None) -> None:
        """Roll one camera's window into the stats tables."""
        stages = metrics.snapshot()
        counters = dict(metrics.counters)
        # Claimed rather than read, so a failure is reported exactly once even if
        # the metrics write itself then fails.
        failed = self.failed_by_camera.pop(metrics.camera_id, 0)
        if failed:
            counters["sightings_write_failed"] = (
                counters.get("sightings_write_failed", 0) + failed
            )
        try:
            with self.connect() as conn, conn.cursor() as cur:
                for s in stages:
                    cur.execute(
                        _STAGE_STATS,
                        (metrics.camera_id, s.stage, s.samples, s.p50_ms, s.p95_ms, s.max_ms),
                    )
                cur.execute(
                    _THROUGHPUT,
                    (
                        metrics.camera_id,
                        counters.get("frames_decoded", 0),
                        counters.get("frames_analysed", 0),
                        counters.get("vehicles_tracked", 0),
                        counters.get("plate_reads", 0),
                        counters.get("sightings_written", 0),
                        decode_fps,
                        json.dumps(
                            {
                                name: counters[name]
                                for name in SECONDARY_COUNTERS
                                if counters.get(name)
                            }
                        ),
                    ),
                )
        except Exception:  # noqa: BLE001 - metrics must never stop the pipeline
            log.exception("failed to write metrics for %s", metrics.camera_id)
