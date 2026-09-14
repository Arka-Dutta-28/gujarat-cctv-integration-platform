-- M9 bonus — camera tamper events.
--
-- A camera that has been covered, defocused or turned keeps delivering a
-- healthy stream: the prober reaches it, frames decode, the frame counter
-- climbs. Every check the platform had before this one passes while the camera
-- sees nothing useful. On an estate of 80,000, nobody walks past most of them
-- to notice.
--
-- Recorded as events rather than as a column on `cameras`, for two reasons. A
-- camera can be tampered with, restored and tampered with again, and the
-- sequence is what an investigator needs. And the detector reports a
-- *suspicion* with the measurement behind it — never an action — so the
-- operator's judgement about each one is itself a record worth keeping.

CREATE TABLE IF NOT EXISTS camera_tamper_events (
    id          BIGSERIAL PRIMARY KEY,
    camera_id   UUID NOT NULL REFERENCES cameras(id),
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind        TEXT NOT NULL CHECK (kind IN ('covered', 'defocused', 'moved')),
    detail      TEXT NOT NULL,
    -- The measurement that triggered it: grey standard deviation, Laplacian
    -- variance or histogram correlation, depending on `kind`. Stored so an
    -- operator can judge the threshold rather than trust it.
    value       REAL,
    acknowledged_by TEXT,
    acknowledged_at TIMESTAMPTZ,
    resolution  TEXT
);

CREATE INDEX IF NOT EXISTS camera_tamper_camera_idx
    ON camera_tamper_events (camera_id, ts DESC);
CREATE INDEX IF NOT EXISTS camera_tamper_open_idx
    ON camera_tamper_events (ts DESC) WHERE acknowledged_at IS NULL;

COMMENT ON TABLE camera_tamper_events IS
    'Suspected camera interference, detected from frames the ANPR pipeline had '
    'already decoded. A suspicion with its measurement attached, never an '
    'action taken automatically.';
