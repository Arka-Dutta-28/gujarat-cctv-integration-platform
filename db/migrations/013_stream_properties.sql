-- 013_stream_properties — carry what the upstream catalogue says about each
-- camera's stream, and how well we know where the camera is.
--
-- Driven by the Sentinel integration reference, which is explicit on two points
-- the registry previously had nowhere to record.
--
-- 1. **The grid is not uniform.** "Cameras differ in resolution, codec, frame
--    rate, and bitrate. Read per-camera properties from /api/ingest and size
--    batching, buffers, and decoders accordingly. A fixed-shape inference batch
--    across every camera will not work unscaled." The pipeline can only do that
--    if the properties are in the registry, because the registry is the only
--    thing an ANPR worker is allowed to read a camera from (invariant 4).
--
-- 2. **Endpoints are data.** Each camera publishes RTSP, WHEP and HLS, and
--    which of them is reachable depends on the network the platform is running
--    on — the reference says to fall back to HLS where 8554 is blocked. So the
--    full set is stored and the capture layer walks it, rather than one URL
--    being stored and a pattern being reconstructed in code.
--
-- JSONB rather than a column per property: the catalogue's field set is the
-- organiser's to change, and the alternative is a migration every time it does.
-- The two properties that actually drive decisions — codec and geometry — get
-- expression indexes so they are still cheap to filter on.

ALTER TABLE cameras
    ADD COLUMN IF NOT EXISTS source_id         TEXT,
    ADD COLUMN IF NOT EXISTS stream_properties JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS endpoints         JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS geo_precision     TEXT,
    ADD COLUMN IF NOT EXISTS catalogue_seen_at TIMESTAMPTZ;

COMMENT ON COLUMN cameras.source_id IS
    'Identifier this camera has in the upstream catalogue. Kept separate from '
    'external_ref because upstream ids can be reused or renumbered, and the '
    'platform''s own reference must not move when they are.';

COMMENT ON COLUMN cameras.stream_properties IS
    'Per-camera stream facts from the upstream catalogue: codec, container, '
    'width, height, declared_fps, bitrate_kbps. declared_fps is recorded for '
    'comparison only — the integration reference warns the reported frame rate '
    'does not match delivery, so all timing comes from measured PTS.';

COMMENT ON COLUMN cameras.endpoints IS
    'Every way to reach this camera, as [{"protocol": "...", "url": "..."}]. '
    'The capture layer walks this ladder and remembers what worked, so a '
    'network where RTSP is blocked degrades to HLS instead of to nothing.';

COMMENT ON COLUMN cameras.geo_precision IS
    'How the position was obtained: landmark, city, district, unplaced, or '
    'survey. Coverage and gap analysis must not treat a district centroid as a '
    'surveyed position, and an operator needs to see which pins still need one.';

COMMENT ON COLUMN cameras.catalogue_seen_at IS
    'Last time the upstream catalogue listed this camera. A camera absent from '
    'the catalogue is marked offline, never deleted — its sightings are '
    'evidence and keep their foreign key.';

CREATE INDEX IF NOT EXISTS cameras_codec_idx
    ON cameras ((stream_properties ->> 'codec'));

CREATE INDEX IF NOT EXISTS cameras_geo_precision_idx
    ON cameras (geo_precision);

CREATE UNIQUE INDEX IF NOT EXISTS cameras_source_id_idx
    ON cameras (source_id) WHERE source_id IS NOT NULL;
