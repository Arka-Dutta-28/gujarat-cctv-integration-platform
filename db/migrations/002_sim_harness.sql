-- 002_sim_harness — test-harness only.
--
-- The registry stays the single source of truth for cameras (invariant 4), so
-- nothing simulator-specific belongs in `cameras`. This table hangs the
-- simulation parameters off a camera row instead: which clip it loops, how far
-- its playback is offset, and which encoding profile it publishes with.
--
-- When the real government feeds arrive, the migration path is: update
-- cameras.stream_ref, delete from camera_sim_config. No application code moves.

CREATE TABLE camera_sim_config (
    camera_id       UUID PRIMARY KEY REFERENCES cameras(id) ON DELETE CASCADE,

    source_file     TEXT NOT NULL,      -- clip looped onto this camera's path
    -- Seconds this camera's playback trails the head of the corridor. Derived
    -- from real inter-camera distance at a plausible corridor speed, so the
    -- resulting journey survives the >120 km/h implausibility check.
    offset_s        INT  NOT NULL DEFAULT 0,

    -- Heterogeneity: the real estate is a mix of resolutions, frame rates and
    -- codecs, and at least one camera is always awful. Discovering the
    -- pipeline's failure modes here is cheaper than discovering them on site.
    profile         TEXT NOT NULL DEFAULT 'baseline',
    width           INT,
    height          INT,
    fps             REAL,
    codec           TEXT,
    extra_args      TEXT[]              -- passed through to ffmpeg verbatim
);

CREATE INDEX camera_sim_profile_idx ON camera_sim_config (profile);
