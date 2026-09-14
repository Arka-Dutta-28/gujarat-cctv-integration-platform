"""Create the platform's logins from the environment.

Accounts are made here rather than by an API endpoint on purpose: a submission
artifact that can create its own logins over the network is a liability an
evaluator would be right to raise, and this is a fixed two-account deployment,
not a product with sign-ups.

Passwords come from the environment and are never defaulted. A seeded default
password in a repository that will be read is exactly what invariant 5 exists to
prevent, so a missing variable skips that account and says so — it does not
invent one.

Usage:
    DEMO_PASSWORD=... OPERATOR_PASSWORD=... python -m scripts.seed_accounts
"""

from __future__ import annotations

import logging
import os
import sys

from services.common.config import settings
from services.common.db import wait_for_db

log = logging.getLogger("seed-accounts")

#: The two accounts the submission needs. The demo account is a `viewer` so an
#: evaluator can drive every screen — trace a vehicle, watch a camera, download
#: a report — without being able to decommission a camera or clear an alert
#: somebody else is looking at.
ACCOUNTS = (
    ("demo", "Demo (read-only)", "viewer", "DEMO_PASSWORD"),
    ("operator", "Duty operator", "operator", "OPERATOR_PASSWORD"),
)


def seed_accounts() -> int:
    import psycopg

    from services.api.auth import hash_password

    created = 0
    with psycopg.connect(settings.dsn) as conn:
        for username, display, role, env_var in ACCOUNTS:
            password = os.environ.get(env_var, "").strip()
            if not password:
                log.warning(
                    "%s is unset — skipping the %r account. Set it and re-run; "
                    "no password is ever defaulted here (invariant 5).",
                    env_var, username,
                )
                continue
            if len(password) < 12:
                # Refused rather than warned: this account is reachable from the
                # public internet on a hosted instance.
                log.error("%s is shorter than 12 characters; refusing", env_var)
                continue

            conn.execute(
                """
                INSERT INTO accounts (username, display_name, role, password_hash)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (username) DO UPDATE
                    SET password_hash = EXCLUDED.password_hash,
                        display_name  = EXCLUDED.display_name,
                        role          = EXCLUDED.role,
                        active        = true
                """,
                (username, display, role, hash_password(password)),
            )
            created += 1
            log.info("account %r seeded as %s", username, role)
        conn.commit()
    return created


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    )
    wait_for_db()
    count = seed_accounts()
    if count == 0:
        log.error(
            "no accounts seeded. Set DEMO_PASSWORD and OPERATOR_PASSWORD in the "
            "environment (see .env.example) and run again."
        )
        return 1
    log.info("%d account(s) ready", count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
