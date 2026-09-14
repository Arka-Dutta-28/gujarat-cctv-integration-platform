-- M3 — ANPR pipeline.
--
-- Three additions, each earned by something the earlier milestones found:
--
-- 1. `slot_offset` locates a detection inside the 12-hour footage slot the
--    organisers' middleware is replaying. Wall-clock time is ours and is
--    correct, but it is not reproducible: an evaluator replaying the same slot
--    tomorrow gets different wall-clock times for the same vehicle. The offset
--    is what makes a result checkable.
-- 2. `condition` because reporting one headline accuracy number across day,
--    night and headlight glare would be indefensible — and three of the four
--    real feeds we have seen are night scenes with severe bloom.
-- 3. `plate_bbox` separately from the vehicle `bbox`, because the crop that
--    goes in the report is the plate, and the box that anchors re-ID is the
--    vehicle.

ALTER TABLE sightings
    ADD COLUMN IF NOT EXISTS slot_offset REAL,
    ADD COLUMN IF NOT EXISTS condition   TEXT,
    ADD COLUMN IF NOT EXISTS plate_bbox  INT[4],
    -- How many frames the track was seen in, as opposed to how many produced a
    -- readable plate (`read_count`). The ratio is a quality signal: a vehicle
    -- tracked for 40 frames that yielded 2 reads is a plate we barely saw.
    ADD COLUMN IF NOT EXISTS track_frames SMALLINT;

COMMENT ON COLUMN sightings.ts IS
    'Ingest time, in UTC. Never the burnt-in overlay clock: the real feeds'
    ' disagree with each other by weeks (docs/field-observations.md §3).';
COMMENT ON COLUMN sightings.slot_offset IS
    'Seconds into the upstream 12-hour playback slot, for reproducibility.';
COMMENT ON COLUMN sightings.condition IS
    'day | night | glare | unknown. Accuracy is reported per condition.';

-- ---------------------------------------------------------------
-- Per-stage timing, rolled up rather than sampled per frame.
--
-- One row per camera per stage per minute. Writing a row per frame would
-- generate more traffic than the sightings themselves and measure the
-- instrumentation; a per-minute rollup keeps the percentiles that matter for
-- the M7 performance page and the HLD sizing without that cost.
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS anpr_stage_stats (
    ts          TIMESTAMPTZ NOT NULL,
    camera_id   UUID NOT NULL REFERENCES cameras(id),
    stage       TEXT NOT NULL,
    samples     INTEGER NOT NULL,
    p50_ms      REAL NOT NULL,
    p95_ms      REAL NOT NULL,
    max_ms      REAL NOT NULL,
    PRIMARY KEY (ts, camera_id, stage)
);

SELECT create_hypertable('anpr_stage_stats', 'ts',
                         chunk_time_interval => INTERVAL '1 day',
                         if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS anpr_stage_stats_stage_idx
    ON anpr_stage_stats (stage, ts DESC);

-- ---------------------------------------------------------------
-- Per-camera throughput, same rollup cadence.
--
-- `frames_decoded` vs `frames_analysed` is the adaptive sampler's work made
-- visible: the gap is the frames we deliberately skipped, which is the single
-- biggest lever in the 80,000-camera sizing argument.
-- ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS anpr_throughput (
    ts               TIMESTAMPTZ NOT NULL,
    camera_id        UUID NOT NULL REFERENCES cameras(id),
    frames_decoded   INTEGER NOT NULL DEFAULT 0,
    frames_analysed  INTEGER NOT NULL DEFAULT 0,
    vehicles_tracked INTEGER NOT NULL DEFAULT 0,
    plate_reads      INTEGER NOT NULL DEFAULT 0,
    sightings_written INTEGER NOT NULL DEFAULT 0,
    decode_fps       REAL,
    PRIMARY KEY (ts, camera_id)
);

SELECT create_hypertable('anpr_throughput', 'ts',
                         chunk_time_interval => INTERVAL '1 day',
                         if_not_exists => TRUE);
