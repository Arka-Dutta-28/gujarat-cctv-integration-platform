-- Mark reads too short to identify a vehicle.
--
-- Invariant 1 requires every plate read to be persisted, and the keep-and-flag
-- convention is explicit that format-validation failures are kept rather than discarded: a
-- mis-read wanted vehicle is worse than a noisy record. Both still hold — this
-- migration adds no filter to the write path and deletes nothing.
--
-- What it adds is the distinction the invariant does not make. The 31
-- government cameras are wide-area situational-awareness views whose plate
-- crops average 66 px across against the simulated farm's 276 px, which yields
-- around 1.7 characters per read. A one-character "plate" is not a partial
-- identification of a vehicle; it cannot narrow a search, cannot support a
-- trace, and in M5 would match a watchlist entry by trigram similarity for no
-- reason. Four characters is the threshold, as a deliberate proxy: `GJ18`
-- narrows a search to a district and is worth keeping in the fuzzy path, while
-- `SS` and `4` are not.
--
-- Everything is still stored and still queryable by exact key. The flag governs
-- only which reads are allowed to *drive* something — fuzzy search and, from
-- M5, alert matching.

ALTER TABLE sightings
    ADD COLUMN IF NOT EXISTS identifying BOOLEAN NOT NULL DEFAULT TRUE;

COMMENT ON COLUMN sightings.identifying IS
    'False when the read is too short to narrow a search (<4 characters). The '
    'row is still persisted and still findable by exact key; the flag keeps '
    'non-identifying noise out of fuzzy search and alert matching.';

-- Backfill against what is already indexed.
UPDATE sightings
   SET identifying = FALSE
 WHERE char_length(plate_normalised) < 4
   AND identifying;

-- Fuzzy search and alerting both filter on this, so it belongs in the index
-- rather than being evaluated per row.
CREATE INDEX IF NOT EXISTS sightings_identifying_trgm_idx
    ON sightings USING gin (plate_normalised gin_trgm_ops)
 WHERE identifying;
