-- M8 — accounts, so the audit trail records people rather than a header.
--
-- Every mutation and every stream view has been audited since M1, with the
-- actor taken from an `X-Actor` header that anyone could set. That was the
-- right order to build it in — wiring the trail end to end first meant no call
-- site had to be revisited — and `services/api/routers/cameras.py` has carried
-- a note since then saying M8 replaces one function and every call site starts
-- recording real identities. This is that migration.
--
-- Two roles, which is all this platform's evidence model needs:
--
--   operator  reads and writes: onboard a camera, watchlist a plate, act on an
--             alert, export a report.
--   viewer    reads only. This is what the submission's demo account uses, so
--             an evaluator can see the whole platform, run a trace, download a
--             report — and cannot decommission a camera or clear an alert.
--
-- Passwords are stored as scrypt hashes with a per-user salt. No dependency:
-- `hashlib.scrypt` is in the standard library, and adding passlib for one call
-- would be a package to audit for nothing. The parameters live with the hash
-- so they can be raised later without invalidating existing rows.

CREATE TABLE IF NOT EXISTS accounts (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT,
    role          TEXT NOT NULL CHECK (role IN ('operator', 'viewer')),
    -- scrypt$n$r$p$<base64 salt>$<base64 hash>. Self-describing on purpose:
    -- the cost parameters can be raised for new accounts while old rows stay
    -- verifiable, which is what makes a future increase a code change rather
    -- than a forced password reset for everyone.
    password_hash TEXT NOT NULL,
    active        BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS accounts_active_idx ON accounts (username) WHERE active;

COMMENT ON TABLE accounts IS
    'Platform logins. The demo account for the submission is a `viewer`: it can '
    'see everything and change nothing.';
COMMENT ON COLUMN accounts.password_hash IS
    'scrypt$n$r$p$salt$hash, base64. Parameters stored per row so they can be '
    'raised without invalidating existing accounts.';
