-- CCTV Integration Platform — core schema
-- PostgreSQL 16 + PostGIS + TimescaleDB

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------
-- Registry (Model 1) — the control plane. Nothing hardcodes a camera.
-- ---------------------------------------------------------------

CREATE TYPE adapter_type  AS ENUM ('rtsp','file','hls','onvif','vendor_sdk','vms_api');
CREATE TYPE camera_status AS ENUM ('online','offline','degraded','unknown','decommissioned');
CREATE TYPE ownership     AS ENUM ('government','private_public_facing');

CREATE TABLE departments (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    code        TEXT NOT NULL UNIQUE
);

CREATE TABLE cameras (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_ref    TEXT UNIQUE,
    name            TEXT NOT NULL,
    department_id   INT REFERENCES departments(id),
    ownership_type  ownership NOT NULL DEFAULT 'government',

    adapter         adapter_type NOT NULL,
    stream_ref      TEXT NOT NULL,              -- URL or path; credentials live in the vault
    credential_ref  TEXT,                       -- vault key, never a secret

    geom            GEOGRAPHY(POINT, 4326) NOT NULL,
    address         TEXT,
    district        TEXT,
    bearing         SMALLINT CHECK (bearing BETWEEN 0 AND 359),
    fov_degrees     SMALLINT CHECK (fov_degrees BETWEEN 1 AND 360),
    range_m         INT,
    mounting_height_m NUMERIC(4,1),

    status          camera_status NOT NULL DEFAULT 'unknown',
    last_seen       TIMESTAMPTZ,
    retention_days  SMALLINT,
    amc_expiry      DATE,
    commissioned_on DATE,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX cameras_geom_idx    ON cameras USING GIST (geom);
CREATE INDEX cameras_status_idx  ON cameras (status);
CREATE INDEX cameras_dept_idx    ON cameras (department_id);

-- ---------------------------------------------------------------
-- Sightings — EVERY plate read, not only watchlist hits.
-- The evaluation plate arrives after the vehicle has passed;
-- without complete history the trace is impossible.
-- ---------------------------------------------------------------

CREATE TABLE sightings (
    id                BIGSERIAL,
    ts                TIMESTAMPTZ NOT NULL,
    camera_id         UUID NOT NULL REFERENCES cameras(id),

    plate_raw         TEXT NOT NULL,            -- exactly what OCR produced
    plate_normalised  TEXT NOT NULL,            -- positional normalisation, see plates.py
    confidence        REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    format_valid      BOOLEAN NOT NULL DEFAULT false,

    track_id          TEXT,                     -- one row per track, not per frame
    read_count        SMALLINT DEFAULT 1,       -- frames voted into this result

    vehicle_class     TEXT,
    vehicle_colour    TEXT,
    bbox              INT[4],
    crop_path         TEXT,
    embedding         VECTOR(256),              -- re-ID, nullable

    PRIMARY KEY (id, ts)
);

SELECT create_hypertable('sightings', 'ts', chunk_time_interval => INTERVAL '1 day');

-- The journey query. This index is what makes the live demo return in <2s.
CREATE INDEX sightings_plate_ts_idx ON sightings (plate_normalised, ts DESC);
CREATE INDEX sightings_camera_ts_idx ON sightings (camera_id, ts DESC);
CREATE INDEX sightings_plate_trgm_idx ON sightings USING GIN (plate_normalised gin_trgm_ops);

ALTER TABLE sightings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'camera_id'
);
SELECT add_compression_policy('sightings', INTERVAL '7 days');

-- ---------------------------------------------------------------
-- Watchlist
-- ---------------------------------------------------------------

CREATE TYPE wl_category AS ENUM ('stolen','wanted','missing','blacklisted','suspect','other');
CREATE TYPE match_tier  AS ENUM ('confirmed','probable','possible');

CREATE TABLE watchlist (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    plate            TEXT NOT NULL,
    plate_normalised TEXT NOT NULL,
    category         wl_category NOT NULL,
    severity         SMALLINT NOT NULL DEFAULT 3 CHECK (severity BETWEEN 1 AND 5),
    source           TEXT,                      -- VAHAN | eGujCop | manual | representative
    case_ref         TEXT,
    notes            TEXT,
    active           BOOLEAN NOT NULL DEFAULT true,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX watchlist_norm_idx ON watchlist (plate_normalised) WHERE active;
CREATE INDEX watchlist_trgm_idx ON watchlist USING GIN (plate_normalised gin_trgm_ops);

-- ---------------------------------------------------------------
-- Alerts
-- ---------------------------------------------------------------

CREATE TYPE alert_status AS ENUM ('new','acknowledged','actioned','dismissed','false_positive');

CREATE TABLE alerts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sighting_id     BIGINT NOT NULL,
    sighting_ts     TIMESTAMPTZ NOT NULL,
    watchlist_id    UUID NOT NULL REFERENCES watchlist(id),
    camera_id       UUID NOT NULL REFERENCES cameras(id),

    tier            match_tier NOT NULL,
    priority        SMALLINT NOT NULL,
    status          alert_status NOT NULL DEFAULT 'new',

    raised_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_by TEXT,
    acknowledged_at TIMESTAMPTZ,
    resolution_note TEXT,

    FOREIGN KEY (sighting_id, sighting_ts) REFERENCES sightings(id, ts)
);

CREATE INDEX alerts_status_idx ON alerts (status, raised_at DESC);
CREATE INDEX alerts_camera_idx ON alerts (camera_id, raised_at DESC);

-- ---------------------------------------------------------------
-- Audit — cheap now, painful to retrofit. Required for the
-- security narrative and for DPDP purpose-binding.
-- ---------------------------------------------------------------

CREATE TABLE audit_log (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,      -- stream.view | plate.search | journey.query | report.export
    subject     TEXT,               -- camera id, plate, report id
    case_ref    TEXT,               -- purpose binding
    detail      JSONB
);

CREATE INDEX audit_ts_idx    ON audit_log (ts DESC);
CREATE INDEX audit_actor_idx ON audit_log (actor, ts DESC);

-- ---------------------------------------------------------------
-- Camera health history — drives the registry map and uptime metrics
-- ---------------------------------------------------------------

CREATE TABLE camera_health (
    ts          TIMESTAMPTZ NOT NULL,
    camera_id   UUID NOT NULL REFERENCES cameras(id),
    status      camera_status NOT NULL,
    fps         REAL,
    latency_ms  INT,
    last_frame_age_s INT,
    tamper      BOOLEAN DEFAULT false
);

SELECT create_hypertable('camera_health', 'ts', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX camera_health_cam_idx ON camera_health (camera_id, ts DESC);
