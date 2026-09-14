"""A vehicle id on every sighting, so a vehicle can be followed without its plate.

Runs after each batch of sightings is committed, in its own transaction: a
link that fails costs a vehicle id, never a sighting (invariant 1).

For each new sighting, in this order:

1. **plate**: a readable plate seen in the window → that sighting's id.
2. **appearance**: the nearest-looking vehicle (learned vector, `reid.embed_many`)
   within `MAX_DISTANCE`, that
   - could have driven between the two cameras in the time (≤ 120 km/h, the
     journey rule),
   - is not a different kind of vehicle (two-wheeler vs car),
   - does not carry a different readable plate,
   - and is clearly nearer than the next-nearest *other* vehicle (`MARGIN`).
   When two vehicles look about equally close, no link is made. A wrong link
   sends an operator after the wrong car, and a missing one only stops the
   trace early.
3. **new**: otherwise the sighting starts a vehicle id of its own.

An appearance link is a lead, not an identification: the link type and
distance are stored with it, and the plate trace never follows one.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from services.anpr.attributes import CLASS_NAMES

log = logging.getLogger("anpr.linking")

__all__ = ["Candidate", "Link", "choose", "link_batch"]

#: Cosine distance on centred DINOv2 vectors (reid.py). Set from 43 links checked
#: by eye on 5 government cameras, 14 Sep 2026: at ≤ 0.05, 16 of 17 were the same
#: object; from 0.05 to 0.10, 20 of 26. A wrong link costs more than a missing one.
MAX_DISTANCE = float(os.environ.get("REID_LINK_DISTANCE", "0.05"))
MARGIN = float(os.environ.get("REID_LINK_MARGIN", "0.03"))
WINDOW_MIN = int(os.environ.get("REID_LINK_WINDOW_MIN", "30"))
MAX_KMH = 120.0


@dataclass(frozen=True)
class Candidate:
    vehicle_uid: int
    seconds_apart: float
    metres: float | None
    distance: float | None
    plate: str
    identifying: bool
    vehicle_class: str | None


@dataclass(frozen=True)
class Link:
    vehicle_uid: int
    via: str  # "plate" | "appearance" | "new"
    distance: float | None = None


def _kind(vehicle_class: str | None) -> str | None:
    name = CLASS_NAMES.get(vehicle_class or "", vehicle_class)
    return None if name is None else ("two-wheeler" if name == "two-wheeler" else "four-wheeler")


def _plausible(new_plate: str, new_identifying: bool, new_class: str | None, c: Candidate) -> bool:
    if new_identifying and c.identifying and new_plate != c.plate:
        return False
    if _kind(new_class) and _kind(c.vehicle_class) and _kind(new_class) != _kind(c.vehicle_class):
        return False
    if c.metres is not None and c.metres > 50:
        return c.metres / max(c.seconds_apart, 1.0) * 3.6 <= MAX_KMH
    return True


def choose(
    own_id: int, plate: str, identifying: bool, vehicle_class: str | None,
    candidates: list[Candidate],
) -> Link:
    """Which vehicle this sighting belongs to. Pure, so the rules are testable."""
    if identifying and plate:
        same = [c for c in candidates if c.identifying and c.plate == plate]
        if same:
            return Link(min(same, key=lambda c: c.seconds_apart).vehicle_uid, "plate")

    nearest: dict[int, float] = {}
    for c in candidates:
        if c.distance is None or not _plausible(plate, identifying, vehicle_class, c):
            continue
        nearest[c.vehicle_uid] = min(c.distance, nearest.get(c.vehicle_uid, 1.0))
    ranked = sorted(nearest.items(), key=lambda item: item[1])
    clear = len(ranked) == 1 or (len(ranked) > 1 and ranked[1][1] - ranked[0][1] >= MARGIN)
    if ranked and ranked[0][1] <= MAX_DISTANCE and clear:
        return Link(ranked[0][0], "appearance", round(ranked[0][1], 4))
    return Link(own_id, "new")


_CANDIDATES = """
SELECT s.vehicle_uid AS uid, abs(extract(epoch FROM n.ts - s.ts)) AS seconds_apart,
       ST_Distance(c.geom, nc.geom) AS metres,
       s.appearance <=> n.appearance AS distance,
       s.plate_normalised AS plate, s.identifying, s.vehicle_class,
       n.plate_normalised AS new_plate, n.identifying AS new_identifying,
       n.vehicle_class AS new_class
  FROM sightings n
  JOIN cameras nc ON nc.id = n.camera_id
  JOIN sightings s ON s.ts BETWEEN n.ts - make_interval(mins => %(window)s)
                               AND n.ts + make_interval(mins => %(window)s)
                  AND s.id <> n.id AND s.vehicle_uid IS NOT NULL
  JOIN cameras c ON c.id = s.camera_id
 WHERE n.id = %(id)s AND n.ts = %(ts)s
   AND ((n.appearance IS NOT NULL AND s.appearance IS NOT NULL
         AND (s.appearance <=> n.appearance) <= %(max)s + %(margin)s)
        OR (n.identifying AND s.identifying AND s.plate_normalised = n.plate_normalised
            AND n.plate_normalised <> ''))
 ORDER BY distance NULLS FIRST
 LIMIT 50
"""

#: Read by name or by position: the worker's connection yields dicts, a plain one tuples.
_NAMES = ("uid", "seconds_apart", "metres", "distance", "plate", "identifying", "vehicle_class",
          "new_plate", "new_identifying", "new_class")

_SET = (
    "UPDATE sightings SET vehicle_uid = %s, uid_via = %s, uid_distance = %s"
    " WHERE id = %s AND ts = %s"
)


def link_batch(cur: Any, written: list[tuple[int, Any]]) -> dict[str, int]:
    """Give each (id, ts) just written a vehicle id. Returns counts by link type."""
    counts = {"plate": 0, "appearance": 0, "new": 0}
    for sighting_id, ts in written:
        cur.execute(_CANDIDATES, {"id": sighting_id, "ts": ts, "window": WINDOW_MIN,
                                  "max": MAX_DISTANCE, "margin": MARGIN})
        rows = [[r[k] for k in _NAMES] if hasattr(r, "keys") else r for r in cur.fetchall()]
        plate, identifying, vehicle_class = (rows[0][7:10] if rows else ("", False, None))
        link = choose(sighting_id, plate, identifying, vehicle_class, [
            Candidate(r[0], float(r[1]), None if r[2] is None else float(r[2]),
                      None if r[3] is None else float(r[3]), r[4], r[5], r[6])
            for r in rows
        ])
        cur.execute(_SET, (link.vehicle_uid, link.via, link.distance, sighting_id, ts))
        counts[link.via] += 1
    return counts


def relink(since: str, until: str, camera_ref: str = "%") -> dict[str, int]:
    """Clear and redo vehicle ids for a window, oldest first.

    For sightings written while linking was off, and for trying a threshold
    (REID_LINK_DISTANCE / REID_LINK_MARGIN) on data already collected:
        python -m services.anpr.linking 2026-09-14T18:13Z 2026-09-14T18:30Z 'sentinel%'
    """
    import psycopg

    from services.common.config import settings

    window = {"since": since, "until": until, "ref": camera_ref}
    scope = """FROM sightings s JOIN cameras c ON c.id = s.camera_id
               WHERE s.ts BETWEEN %(since)s AND %(until)s AND c.external_ref LIKE %(ref)s"""
    with psycopg.connect(settings.dsn) as conn, conn.cursor() as cur:
        cur.execute(f"""UPDATE sightings SET vehicle_uid = NULL, uid_via = NULL, uid_distance = NULL
                        WHERE (id, ts) IN (SELECT s.id, s.ts {scope})""", window)
        cur.execute(f"SELECT s.id, s.ts {scope} ORDER BY s.ts, s.id", window)
        return link_batch(cur, cur.fetchall())


if __name__ == "__main__":
    import sys

    print(relink(*sys.argv[1:4]))
