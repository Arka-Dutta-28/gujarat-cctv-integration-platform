-- 003_real_feeds — support the government feed estate.
--
-- Driven by what the 31 evaluation cameras actually turned out to be
-- (docs/field-observations.md). Two additions:
--
-- 1. `http` as an adapter. The feeds are delivered as progressive HTTP byte
--    ranges — container mp4/mkv/avi, codec h264 — not RTSP and not HLS. The
--    existing `hls` value would be a lie: there is no playlist, `hls_url` is
--    null on every camera, and a client that went looking for one would fail.
--
-- 2. `camera_kind`. Device labels on the real feeds distinguish PTZ units
--    (`CSITMS-32_PTZ2`, `Majevadi Gate PTZ-2`) from fixed ones (`FIX-3`). For a
--    PTZ, bearing/fov/range describe the current preset rather than the camera,
--    so coverage and gap analysis must treat it differently instead of drawing
--    a confidently wrong wedge.

ALTER TYPE adapter_type ADD VALUE IF NOT EXISTS 'http';

CREATE TYPE camera_kind AS ENUM ('fixed', 'ptz', 'unknown');

ALTER TABLE cameras
    ADD COLUMN kind camera_kind NOT NULL DEFAULT 'unknown';

COMMENT ON COLUMN cameras.kind IS
    'PTZ cameras move, so their bearing/fov/range describe the current preset, '
    'not the camera. Coverage analysis must special-case them.';

CREATE INDEX cameras_kind_idx ON cameras (kind);
