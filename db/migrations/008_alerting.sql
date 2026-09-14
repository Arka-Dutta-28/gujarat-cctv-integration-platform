-- M5 — live watchlist alerting.
--
-- The tables themselves are from 001; what they were missing is the access
-- pattern this milestone actually uses.
--
-- The alert console's live push polls for "anything raised since the last time
-- I looked", which is a bare `raised_at >` with no status predicate. The
-- existing indexes are both composite on something else first
-- (`status, raised_at DESC` and `camera_id, raised_at DESC`), so that query had
-- no usable index and degrades into a scan as the table grows — on a path that
-- runs once a second for as long as an operator has the console open.

CREATE INDEX IF NOT EXISTS alerts_raised_idx ON alerts (raised_at DESC);

-- Duplicate suppression is per worker process and in memory (see
-- services/alerting/writer.py), which is deliberate: a database round trip per
-- sighting on the pipeline's write path would cost more than the duplicates do.
-- The trade is that a worker restart can re-alert a vehicle once per camera.
-- This constraint bounds the damage — the *same sighting* can never raise the
-- same alert twice, whatever happens above it.
CREATE UNIQUE INDEX IF NOT EXISTS alerts_once_per_sighting_idx
    ON alerts (sighting_id, watchlist_id);

COMMENT ON TABLE alerts IS
    'Watchlist matches raised on the sighting write path. Rows are never '
    'deleted: dismissed and false_positive are statuses, because an operator '
    'judging a match wrong is evidence and is the only basis for an honest '
    'false-positive rate.';
