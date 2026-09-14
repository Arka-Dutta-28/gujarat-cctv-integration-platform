"""Turn a written sighting into an alert, in the same transaction that wrote it.

Why here and not in a consumer. db/migrations/001_init.sql records that `alerts`
cannot carry a foreign key to `sightings`, because TimescaleDB does not support
foreign keys referencing a hypertable, and that integrity for that edge is
enforced instead by the alerting writer only ever inserting an alert for a
sighting it has just written in the same transaction. This module is that
promise made real. An alert can never point at a sighting that does not exist,
because the two rows commit together or neither does.

It also settles the latency question by construction. The M5 budget is five
seconds from detection to an alert on screen; a separate consumer would spend
part of that budget on a queue hop and a poll interval before it started
matching. Matching against an in-memory watchlist costs microseconds on a path
that is already writing to the database.

This does not merge the two code paths (invariant 2). Alerting reads a sighting
as it streams past and evaluates a match; trace queries the historical index by
plate. They share the sightings table, which is the point, and no code.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from services.alerting.tiers import (
    ATTRIBUTE,
    POSSIBLE,
    Match,
    match_attributes,
    match_plate,
)
from services.alerting.watchlist import WatchlistCache
from services.anpr.attributes import CLASS_NAMES

log = logging.getLogger("alerting.writer")

__all__ = ["AlertWriter", "WrittenSighting", "RaisedAlert", "DEDUP_WINDOW_S"]

#: How long the same watchlist entry is suppressed at the same camera.
#:
#: The pipeline cuts a track every 30 seconds so a parked vehicle's plate is not
#: withheld until it drives away (an M3 decision). A wanted car stopped in view
#: of one camera therefore produces a sighting every 30 seconds, and without
#: suppression an operator would get an alert every 30 seconds for a vehicle
#: they have already been told about. Keyed on *(entry, camera)* rather than on
#: the entry alone, so the same vehicle appearing at the **next** camera alerts
#: immediately — that movement is the thing worth knowing.
DEDUP_WINDOW_S = float(os.environ.get("ALERT_DEDUP_WINDOW_S", "120"))

#: `possible` matches are two characters out. They are still shown, because a
#: wanted vehicle read badly is still a wanted vehicle — but they repeat more
#: (any of several plates can land two characters from an entry), so they are
#: suppressed for longer.
POSSIBLE_DEDUP_MULTIPLIER = 3.0

#: How long a plate match keeps corroborating appearance matches for the same
#: watchlist entry.
#:
#: This window is the whole safety property of the `attribute` tier, so it is
#: reasoned rather than picked. It answers: having just read a wanted vehicle's
#: plate at one camera, for how long is "a vehicle of that description" at
#: *any* camera more likely to be that vehicle than a coincidence? Ten minutes
#: at the ~60 km/h these corridors carry is a ~10 km radius, which is inside
#: the range where the journey plausibility check would accept the transition
#: anyway. Beyond that the population of same-coloured vehicles grows faster
#: than the evidence does, and the tier goes quiet — which is the intended
#: failure mode. Lengthen this and it becomes a colour alarm.
CORROBORATION_WINDOW_S = float(os.environ.get("ALERT_CORROBORATION_WINDOW_S", "600"))

#: Appearance matches repeat hardest of all — every white truck on the road is
#: a candidate — so they are suppressed for longest.
ATTRIBUTE_DEDUP_MULTIPLIER = 5.0

#: How long each tier is suppressed for, as a multiple of the base window.
#: One lookup rather than a conditional per tier, so adding a fifth tier is a
#: line here instead of an `elif` someone forgets.
_DEDUP_MULTIPLIER = {
    POSSIBLE: POSSIBLE_DEDUP_MULTIPLIER,
    ATTRIBUTE: ATTRIBUTE_DEDUP_MULTIPLIER,
}


@dataclass(frozen=True)
class WrittenSighting:
    """A sighting as the database returned it, id and all."""

    id: int
    ts: datetime
    camera_id: str
    plate_normalised: str
    confidence: float
    identifying: bool
    #: What the vehicle looked like. Present on every row from M15 onward and
    #: absent on everything written before it, so both are handled.
    vehicle_colour: str | None = None
    vehicle_class: str | None = None

    @property
    def read_a_plate(self) -> bool:
        """Whether this row carries a plate at all.

        An attribute-only row stores the empty string rather than NULL, because
        the column has been NOT NULL since 001. This is the one place that
        distinction is interpreted, so nothing downstream has to know it.
        """
        return bool((self.plate_normalised or "").strip())


@dataclass(frozen=True)
class RaisedAlert:
    sighting_id: int
    camera_id: str
    match: Match


_INSERT_ALERT = """
INSERT INTO alerts (sighting_id, sighting_ts, watchlist_id, camera_id, tier, priority)
VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (sighting_id, watchlist_id) DO NOTHING
"""
# `DO NOTHING` rather than letting the unique index raise: a duplicate is a
# no-op the writer should absorb quietly, and an exception here is caught by the
# same handler that guards against a real database fault, where the noise
# matters.


@dataclass
class AlertWriter:
    """Matches written sightings against the watchlist and records the hits."""

    watchlist: WatchlistCache
    dedup_window_s: float = DEDUP_WINDOW_S
    raised: int = 0
    suppressed: int = 0
    #: (watchlist_id, camera_id) -> monotonic time of the last alert raised.
    #: Per process, so a worker restart re-alerts once per vehicle per camera.
    #: Deliberate: losing an alert is far worse than repeating one, and the
    #: alternative is a database round trip on the write path.
    _recent: dict[tuple[str, str], float] = field(default_factory=dict)
    #: watchlist_id -> monotonic time its *plate* was last matched. Only these
    #: entries can be matched on appearance, and only while the entry is inside
    #: `CORROBORATION_WINDOW_S`. Held here rather than queried, for the same
    #: reason as `_recent`: this is the write path, and a round trip per unread
    #: vehicle on a busy road would cost more than the alert is worth.
    _corroborated: dict[str, float] = field(default_factory=dict)

    def consider(
        self, cur: Any, rows: list[WrittenSighting], now: float | None = None
    ) -> list[RaisedAlert]:
        """Raise alerts for whatever in `rows` matches. Never raises upward.

        Called with the cursor that just wrote the sightings, inside its
        transaction. An exception here would roll back the sightings themselves,
        which would trade an alerting failure for a breach of invariant 1 — so
        every failure is logged and swallowed.
        """
        entries = self.watchlist.entries()
        if not entries or not rows:
            return []

        now = time.monotonic() if now is None else now
        raised: list[RaisedAlert] = []
        for row in rows:
            try:
                if row.read_a_plate:
                    hit = match_plate(
                        row.plate_normalised, row.confidence, entries,
                        identifying=row.identifying,
                    )
                else:
                    # No plate at all — a wide-area camera that saw a vehicle
                    # and could not read it. The only thing left to match on is
                    # what it looked like, and that is worth doing *only* for
                    # entries whose plate was matched a moment ago.
                    hit = match_attributes(
                        row.vehicle_colour,
                        # `sightings.vehicle_class` stores the detector's own
                        # COCO label; a watchlist entry is written in the words
                        # a person would use. Mapped here, at the one point the
                        # two meet, rather than by changing what either stores.
                        CLASS_NAMES.get((row.vehicle_class or "").strip().lower()),
                        entries,
                        corroborated=self._corroborating(now),
                    )
            except Exception:  # noqa: BLE001 - one bad read must not stop the batch
                log.exception("match failed for sighting %s", row.id)
                continue
            if hit is None:
                continue

            key = (hit.entry.id, row.camera_id)
            window = self.dedup_window_s * _DEDUP_MULTIPLIER.get(hit.tier, 1.0)
            last = self._recent.get(key)
            if last is not None and (now - last) < window:
                self.suppressed += 1
                continue

            try:
                cur.execute(
                    _INSERT_ALERT,
                    (row.id, row.ts, hit.entry.id, row.camera_id, hit.tier, hit.priority),
                )
            except Exception:  # noqa: BLE001
                log.exception("failed to record alert for sighting %s", row.id)
                continue

            self._recent[key] = now
            if hit.tier != ATTRIBUTE:
                # Only a plate match corroborates. An appearance match
                # extending the window would let one plate read license an
                # unbounded chain of colour alerts, each one corroborated by
                # the last — which is how a safeguard becomes a rubber stamp.
                self._corroborated[hit.entry.id] = now
            self.raised += 1
            raised.append(RaisedAlert(row.id, row.camera_id, hit))
            log.warning(
                "ALERT %s (%s, severity %d) at camera %s — %s",
                hit.entry.plate, hit.tier, hit.entry.severity, row.camera_id, hit.reason,
            )

        self._forget_stale(now)
        return raised

    def _corroborating(self, now: float) -> set[str]:
        """Entries whose plate was matched recently enough to support appearance."""
        return {
            entry_id
            for entry_id, seen in self._corroborated.items()
            if now - seen < CORROBORATION_WINDOW_S
        }

    def _forget_stale(self, now: float) -> None:
        """Bound the suppression map. A long-running worker sees many vehicles."""
        self._corroborated = {
            k: t for k, t in self._corroborated.items()
            if now - t < CORROBORATION_WINDOW_S
        }
        horizon = self.dedup_window_s * ATTRIBUTE_DEDUP_MULTIPLIER
        if len(self._recent) < 4096:
            return
        self._recent = {k: t for k, t in self._recent.items() if now - t < horizon}
