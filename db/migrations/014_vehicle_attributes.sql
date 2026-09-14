-- Describe the vehicles that cannot be read, and let a description corroborate.
--
-- The problem this closes is measured, not anticipated. `/api/cameras/
-- anpr-capability` grades 0 of the 30 government cameras at ANPR grade: their
-- plate crops average 66 px across against the 80 px the OCR needs, and a
-- 45-minute run over all thirty produced 20 valid plates from 4,442 reads.
-- Before this migration, a camera that could not read a plate wrote nothing at
-- all — so thirty cameras watched thousands of vehicles go past and left no
-- record that any of them existed.
--
-- Colour and coarse class survive at crop sizes where OCR is hopeless, because
-- they need tens of pixels rather than hundreds. This migration is where they
-- are stored, on both sides of the match.
--
-- WHAT THIS IS NOT. A colour is not an identification. Several thousand white
-- hatchbacks pass a Gujarat highway camera in a day. Nothing here weakens
-- invariant 1 (every plate read is still persisted unconditionally) or
-- invariant 2 (trace and alerting remain separate paths); the `attribute` tier
-- added below is the weakest tier there is, it can never originate an alert,
-- and services/alerting/tiers.py documents the three constraints that make it
-- safe to have at all.

-- --------------------------------------------------------------------
-- 1. What a sighting looked like
-- --------------------------------------------------------------------

-- `vehicle_colour` has existed since 001 and has never been written. It is
-- populated from here on.
COMMENT ON COLUMN sightings.vehicle_colour IS
    'What a person would call this vehicle''s colour, from services/anpr/'
    'attributes.py. NULL means the platform declined to name one — a crop too '
    'small, too dark or too mixed to support a name — never "no colour".';

ALTER TABLE sightings
    ADD COLUMN IF NOT EXISTS colour_confidence REAL;

COMMENT ON COLUMN sightings.colour_confidence IS
    'Share of visible bodywork holding the named colour. Below '
    'ANPR_MIN_COLOUR_CONFIDENCE the colour is not recorded at all, so a row '
    'with a colour always has a confidence at or above that floor.';

-- Attribute-only rows carry an empty plate, so the plate indexes must not be
-- asked to serve attribute queries and vice versa. This is the index for
-- "silver cars at this camera in this hour", which is the query the government
-- estate can actually answer.
CREATE INDEX IF NOT EXISTS sightings_attributes_idx
    ON sightings (vehicle_colour, vehicle_class, ts DESC)
 WHERE vehicle_colour IS NOT NULL;

-- The plate indexes gain nothing from rows whose plate is empty, and on the
-- government cameras those rows will outnumber the real reads by orders of
-- magnitude. Partial indexes keep the journey query — which must return in
-- under 2 seconds in front of evaluators — reading only rows that have a plate.
CREATE INDEX IF NOT EXISTS sightings_plate_ts_read_idx
    ON sightings (plate_normalised, ts DESC)
 WHERE plate_normalised <> '';

-- --------------------------------------------------------------------
-- 2. What a wanted vehicle looks like
-- --------------------------------------------------------------------

-- Almost always NULL, and that is correct rather than a gap to backfill. VAHAN
-- carries a colour; a manually-entered "suspect" row usually does not. An
-- entry with no description can never be matched on appearance, which is the
-- safe default — `match_attributes` requires BOTH fields before it will
-- consider an entry at all.
ALTER TABLE watchlist
    ADD COLUMN IF NOT EXISTS vehicle_colour TEXT,
    ADD COLUMN IF NOT EXISTS vehicle_class  TEXT;

COMMENT ON COLUMN watchlist.vehicle_colour IS
    'Described colour of the wanted vehicle, in the vocabulary of '
    'services/anpr/attributes.py COLOUR_NAMES. NULL disables appearance '
    'matching for this entry.';
COMMENT ON COLUMN watchlist.vehicle_class IS
    'Described class in human words (car, truck, bus, two-wheeler, '
    'three-wheeler), not the detector''s COCO label. NULL disables appearance '
    'matching for this entry.';

-- --------------------------------------------------------------------
-- 3. The fourth tier
-- --------------------------------------------------------------------

-- Postgres allows ADD VALUE inside a transaction from 12 onwards (this estate
-- runs 16), provided the new value is not *used* in the same transaction.
-- Nothing below uses it, and the migration runner commits each file on its
-- own, so it is available to the application from the next statement onward.
ALTER TYPE match_tier ADD VALUE IF NOT EXISTS 'attribute';
