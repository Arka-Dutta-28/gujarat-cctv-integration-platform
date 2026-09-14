"""The watchlist the matcher holds in memory, and how it stays fresh.

Every sighting write is matched against every active watchlist entry, so the
list cannot be a query per sighting: at 104 sightings a minute across the estate
that is a round trip per read on the hot path of the pipeline. It is held in
process and refreshed on a timer.

The refresh interval is the alerting equivalent of a staleness budget. A plate
added to the watchlist must raise an alert on the *next* sighting of that
vehicle, and the M5 acceptance measures exactly that end to end, so the window
between "an operator adds a plate" and "every worker knows about it" is part of
the graded latency. 10 seconds against a 5-second detection budget would fail;
2 seconds costs one small query per worker per 2 seconds, which is nothing
against the 20 cores the pipeline is already using.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from services.alerting.tiers import WatchlistEntry

log = logging.getLogger("alerting.watchlist")

__all__ = ["WatchlistCache", "REFRESH_S", "load_watchlist"]

#: How long a worker may hold a stale copy of the watchlist. See module docs.
REFRESH_S = float(os.environ.get("ALERT_WATCHLIST_REFRESH_S", "2"))

_SELECT = """
SELECT id::text AS id, plate, plate_normalised, category::text AS category,
       severity, case_ref, vehicle_colour, vehicle_class
  FROM watchlist
 WHERE active
"""


def load_watchlist(connect: Any) -> list[WatchlistEntry]:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(_SELECT)
        return [
            WatchlistEntry(
                id=r["id"], plate=r["plate"], plate_normalised=r["plate_normalised"],
                category=r["category"], severity=r["severity"], case_ref=r["case_ref"],
                vehicle_colour=r["vehicle_colour"], vehicle_class=r["vehicle_class"],
            )
            for r in cur.fetchall()
        ]


class WatchlistCache:
    """Active watchlist entries, refreshed on a timer.

    On a refresh failure the previous copy is kept and the error logged. The
    alternative — an empty list — would silently stop all alerting during a
    database blip, which is the failure an operator is least able to notice.
    """

    def __init__(
        self,
        connect: Any,
        refresh_s: float = REFRESH_S,
        loader: Any = load_watchlist,
    ) -> None:
        self._connect = connect
        self._refresh_s = refresh_s
        self._loader = loader
        self._entries: list[WatchlistEntry] = []
        self._loaded_at: float | None = None
        self.loads = 0
        self.failures = 0

    def entries(self, now: float | None = None) -> list[WatchlistEntry]:
        now = time.monotonic() if now is None else now
        if self._loaded_at is None or (now - self._loaded_at) >= self._refresh_s:
            self._reload(now)
        return self._entries

    def _reload(self, now: float) -> None:
        try:
            self._entries = self._loader(self._connect)
            self.loads += 1
        except Exception:  # noqa: BLE001 - keep the last good copy
            self.failures += 1
            log.exception("watchlist refresh failed; matching against the last copy")
        finally:
            # Stamped even on failure, so a database that is down does not turn
            # into a retry storm on the pipeline's hot path.
            self._loaded_at = now

    @property
    def size(self) -> int:
        return len(self._entries)
