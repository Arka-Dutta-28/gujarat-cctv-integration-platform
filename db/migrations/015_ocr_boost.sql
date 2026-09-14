-- Per-camera OCR boost: an operator asks for the GPU reader on chosen cameras.
--
-- The estate reads plates with docTR on the CPU. PaddleOCR-VL reads more — on
-- the generated night clips it found 101 of 101 plates against docTR's 63 —
-- but costs ~440 ms per crop on a GPU and 12.5 s on a CPU (measured 14 Sep
-- 2026), so it cannot run everywhere. It runs where an operator asks: the
-- cameras around the place a vehicle has been narrowed down to.
--
-- A boost is a request, not a setting. It always expires, it can be cleared,
-- and the worker writes back whether it actually took effect — a boost the
-- operator believes is running on a worker with no GPU is the failure this
-- table exists to make visible.

CREATE TABLE IF NOT EXISTS camera_ocr_boosts (
    id           BIGSERIAL PRIMARY KEY,
    camera_id    UUID NOT NULL REFERENCES cameras(id),
    backend      TEXT NOT NULL CHECK (backend IN ('paddleocr-vl')),
    requested_by TEXT NOT NULL,
    case_ref     TEXT,
    reason       TEXT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    cleared_at   TIMESTAMPTZ,
    cleared_by   TEXT,
    -- Written by the ANPR worker that owns the camera.
    applied_at   TIMESTAMPTZ,
    apply_error  TEXT,
    CHECK (expires_at > requested_at)
);

-- Workers poll for the live ones every few seconds.
CREATE INDEX IF NOT EXISTS camera_ocr_boosts_open
    ON camera_ocr_boosts (camera_id, expires_at)
    WHERE cleared_at IS NULL;

COMMENT ON TABLE camera_ocr_boosts IS
    'Operator requests to read chosen cameras with a heavier OCR model for a '
    'limited time. Active = cleared_at IS NULL AND expires_at > now(). '
    'applied_at / apply_error are the owning worker''s answer.';
