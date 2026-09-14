-- A vehicle id on every sighting, so a vehicle can be followed across cameras
-- when its plate cannot be read.
--
-- On the government cameras plates are ~29 px and no reader gets them (14 Sep
-- 2026), so a trace built on plates alone stops at the first camera. This gives
-- each sighting a `vehicle_uid`:
--
--   plate       a readable plate already seen in the last half hour → that id
--   appearance  a vehicle that looks the same (learned image embedding), on a
--               camera it could have driven from in the time, and clearly
--               closer than any other candidate → that id
--   new         neither → the sighting's own id
--
-- `appearance` is a vector from DINOv2-small (384 numbers), a learned image
-- model. It sits beside the older 64-number colour histogram in `embedding`
-- rather than replacing it: images without the model still write that one.
-- An appearance link is a lead for an operator to check against the crops,
-- which is why the link type and distance are stored with it.

ALTER TABLE sightings
    ADD COLUMN IF NOT EXISTS appearance   vector(384),
    ADD COLUMN IF NOT EXISTS vehicle_uid  BIGINT,
    ADD COLUMN IF NOT EXISTS uid_via      TEXT,
    ADD COLUMN IF NOT EXISTS uid_distance REAL;

CREATE INDEX IF NOT EXISTS sightings_vehicle_uid_idx ON sightings (vehicle_uid, ts DESC)
    WHERE vehicle_uid IS NOT NULL;
