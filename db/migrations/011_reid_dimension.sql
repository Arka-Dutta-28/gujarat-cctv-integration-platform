-- Correct the re-ID vector's dimension, and record why 010 did nothing.
--
-- `001_init.sql` already declared `sightings.embedding VECTOR(256)` — a
-- placeholder sized for a learned re-identification model, since 128/256/512
-- is what those produce. Migration 010 then wrote
-- `ADD COLUMN IF NOT EXISTS embedding vector(64)`, which was a **silent no-op**
-- because the column already existed. Nothing failed at migration time; the
-- failure surfaced later as `expected 256 dimensions, not 64` on every single
-- INSERT, which took the whole estate's write path down until `write_health`
-- reported it.
--
-- The lesson is about `IF NOT EXISTS`: it makes a migration idempotent, and it
-- also makes a *conflicting* definition invisible. It is the right tool for
-- re-runnable DDL and the wrong one for changing something that already exists.
--
-- 64 is the dimension the descriptor actually produces: 32 hue + 16 saturation
-- + 8 value bins, plus an 8-band vertical brightness profile
-- (`services/anpr/reid.py`). The learned model the 256 was reserved for could
-- not be obtained on this network, and sizing the column for a model that is
-- not here would store 192 zeroes on every row of a hypertable.
--
-- Safe as a type change rather than a drop: no row carries an embedding yet.

ALTER TABLE sightings
    ALTER COLUMN embedding TYPE vector(64);

COMMENT ON COLUMN sightings.embedding IS
    'Appearance descriptor (HSV histograms + vertical brightness profile), '
    'L2-normalised, 64 dimensions. A colour-and-shape signature for '
    'shortlisting candidate matches, NOT a learned re-identification '
    'embedding: it finds vehicles that look alike, not vehicles that are '
    'provably the same. Raising this to a learned model is a change to '
    'services/anpr/reid.py plus one migration for the dimension.';
