-- Persist the counters that justify the pipeline's design decisions.
--
-- `anpr_throughput` has a fixed column per counter, so four counters that the
-- worker already maintains were being discarded at every rollup:
--
--   ocr_skipped_settled     -- tracks whose vote had settled, so OCR was skipped
--   ocr_shed                -- reads dropped rather than queued under load
--   via_relay               -- sessions that fell back to the relayed stream
--   motion_fallback_windows -- windows where the learned detector saw nothing
--
-- Each of those numbers is the evidence for a design decision: the per-track
-- read budget, shedding rather than queueing, the relay
-- fallback for feeds OpenCV cannot open, and the composite tracker's switch.
-- A design justified by a counter nobody can read is justified by assertion.
--
-- JSONB rather than four more columns, deliberately: the set of interesting
-- counters is still moving, and a new one should not need a migration. The five
-- counters that are load-bearing for `/api/performance` keep their typed
-- columns, because those are queried in aggregate on every request.

ALTER TABLE anpr_throughput
    ADD COLUMN IF NOT EXISTS counters JSONB NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN anpr_throughput.counters IS
    'Secondary pipeline counters for one camera-minute. Additive across '
    'rollups, like the typed columns beside it.';
