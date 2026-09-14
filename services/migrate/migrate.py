"""Forward-only migration runner.

Applies `db/migrations/NNN_*.sql` in lexical order inside a transaction each,
and records a checksum per file. Re-running an already-applied migration whose
contents have changed is a hard error — that is the convention "migrations are
forward-only; never edit an applied migration" turned into something the build
actually enforces rather than something a reviewer has to notice.

Usage:
    python -m services.migrate.migrate            # apply pending
    python -m services.migrate.migrate --status   # report, change nothing
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg

from services.common.config import settings
from services.common.db import wait_for_db

log = logging.getLogger("migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"
_FILENAME_RE = re.compile(r"^(\d+)_[a-z0-9_]+\.sql$")

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    duration_ms INT
);
"""


@dataclass(frozen=True)
class Migration:
    version: str
    filename: str
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Load migrations in version order, rejecting anything misnamed."""
    if not directory.is_dir():
        raise FileNotFoundError(f"no migrations directory at {directory}")

    found: list[Migration] = []
    seen: dict[str, str] = {}
    for path in sorted(directory.glob("*.sql")):
        m = _FILENAME_RE.match(path.name)
        if not m:
            raise ValueError(
                f"migration {path.name!r} is misnamed; expected NNN_lower_snake.sql"
            )
        version = m.group(1)
        if version in seen:
            raise ValueError(
                f"duplicate migration version {version}: {seen[version]} and {path.name}"
            )
        seen[version] = path.name
        found.append(
            Migration(
                version=version,
                filename=path.name,
                path=path,
                sql=path.read_text(encoding="utf-8"),
            )
        )
    return found


def _applied(conn: psycopg.Connection) -> dict[str, tuple[str, str]]:
    rows = conn.execute(
        "SELECT version, filename, checksum FROM schema_migrations"
    ).fetchall()
    # Rows come back as tuples here: this runs before the pool's dict row factory.
    return {r[0]: (r[1], r[2]) for r in rows}


def _verify_unchanged(pending: list[Migration], applied: dict[str, tuple[str, str]]) -> None:
    drifted = [
        f"  {m.filename}: recorded {applied[m.version][1][:12]}, file is {m.checksum[:12]}"
        for m in pending
        if m.version in applied and applied[m.version][1] != m.checksum
    ]
    if drifted:
        raise RuntimeError(
            "an already-applied migration has been edited, which is forward-only "
            "violation:\n" + "\n".join(drifted) + "\n"
            "Write a new migration that alters the schema instead of editing this one."
        )


def status() -> int:
    wait_for_db()
    with psycopg.connect(settings.dsn) as conn:
        conn.execute(_BOOTSTRAP)
        conn.commit()
        applied = _applied(conn)
        migrations = discover()
        _verify_unchanged(migrations, applied)
        for m in migrations:
            mark = "applied" if m.version in applied else "PENDING"
            print(f"  {m.version}  {mark:8}  {m.filename}")
        pending = [m for m in migrations if m.version not in applied]
        print(f"\n{len(applied)} applied, {len(pending)} pending")
    return 0


def migrate() -> int:
    import time

    wait_for_db()
    with psycopg.connect(settings.dsn) as conn:
        conn.execute(_BOOTSTRAP)
        conn.commit()
        applied = _applied(conn)
        migrations = discover()
        _verify_unchanged(migrations, applied)

        pending = [m for m in migrations if m.version not in applied]
        if not pending:
            log.info("schema up to date (%d applied)", len(applied))
            return 0

        for m in pending:
            log.info("applying %s", m.filename)
            started = time.monotonic()
            try:
                # Each migration is one transaction: it applies whole or not at all.
                conn.execute(m.sql)
            except Exception:
                conn.rollback()
                log.error("migration %s failed; rolled back", m.filename)
                raise
            elapsed_ms = int((time.monotonic() - started) * 1000)
            conn.execute(
                "INSERT INTO schema_migrations (version, filename, checksum, duration_ms)"
                " VALUES (%s, %s, %s, %s)",
                (m.version, m.filename, m.checksum, elapsed_ms),
            )
            conn.commit()
            log.info("applied  %s in %dms", m.filename, elapsed_ms)

        log.info("%d migration(s) applied", len(pending))
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description="Apply database migrations.")
    parser.add_argument(
        "--status", action="store_true", help="report state without applying anything"
    )
    args = parser.parse_args()
    return status() if args.status else migrate()


if __name__ == "__main__":
    sys.exit(main())
