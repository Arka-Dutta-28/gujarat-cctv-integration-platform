"""Turn a plate's sightings into a movement history.

The shape of the problem, and why the code looks like this.

A sighting is not a visit. A vehicle held in one camera's view, stopped at a
light or queuing at a toll, produces a new track every 30 seconds by design,
because the pipeline cuts long tracks so a parked car's plate is not withheld
until it drives away. Rendering those as separate journey points would draw a
vehicle teleporting on the spot forty times. Consecutive sightings on one camera
are therefore collapsed into a Visit with a first- and last-seen time, which is
also the honest description: the camera saw the vehicle over an interval.

An implausible hop splits the journey, it does not get dropped. If the implied
speed between two visits exceeds about 120 km/h the two cannot be the same
vehicle moving normally, which is exactly how a cloned plate surfaces and is the
single most useful thing this endpoint can tell an investigator. The journey is
cut into Legs at that point and the overall confidence is lowered, but every
sighting stays in the response. Silently discarding the outlier would delete the
finding.

Confidence is a claim about the whole journey, not an average. It starts from
how well the plates were read and is degraded by every implausible transition,
because a route assembled from confident reads that could not physically have
happened is not a confident route.

Everything here is pure: no database, no HTTP, no clock. Road distances are
passed in by the caller, so the same logic covers the OSRM-snapped case and the
great-circle fallback, and so the interesting behaviour can be tested without a
routing engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from services.common.geo import (
    IMPLAUSIBLE_SPEED_KMH,
    Point,
    haversine_m,
    implied_speed_kmh,
)

__all__ = [
    "Hop",
    "Journey",
    "Leg",
    "Visit",
    "REVISIT_WINDOW_S",
    "collapse_revisits",
    "reconstruct",
]

#: Consecutive sightings on the same camera closer together than this are one
#: visit. Chosen against the pipeline rather than guessed: a track is cut and
#: emitted every 30 s, so anything under a couple of minutes is the same vehicle
#: still in view. Beyond it, a vehicle that genuinely left and came back is
#: shown as two visits — which for a trace is the more useful reading.
REVISIT_WINDOW_S = 180.0

#: How much an implausible hop costs the journey's overall confidence. One is a
#: strong signal and should be visible; three should not drive confidence
#: negative, so the penalty is applied multiplicatively.
IMPLAUSIBLE_PENALTY = 0.6


@dataclass(frozen=True)
class Visit:
    """One camera's observation of the vehicle over an interval."""

    camera_id: str
    camera_name: str | None
    district: str | None
    point: Point
    first_seen: datetime
    last_seen: datetime
    sightings: int
    confidence: float
    sighting_ids: list[int] = field(default_factory=list)
    thumbnail_url: str | None = None

    @property
    def dwell_s(self) -> float:
        return (self.last_seen - self.first_seen).total_seconds()


@dataclass(frozen=True)
class Hop:
    """The move between two consecutive visits.

    Two distances are carried, and which one decides plausibility matters.

    `direct_distance_m` is the great-circle line: a hard lower bound on how far
    the vehicle went, since nothing travels less than the straight line.
    `road_distance_m` is what OSRM says the drive is, which is more useful to
    show and less safe to judge by — it depends on both cameras snapping to the
    right road. Measured on this estate, snapped distances ran 1.5x to 2.1x the
    direct line, because cameras interpolated along a coarse corridor snap to
    service roads and the route detours around them.

    So plausibility is judged on the direct distance. That makes the clone
    detector conservative by construction: it only fires when the vehicle could
    not have covered even the shortest possible path in the time available,
    which is a claim about geometry rather than about the quality of a map
    match. Both speeds are reported, so an operator can see the difference.
    """

    from_camera: str
    to_camera: str
    direct_distance_m: float
    road_distance_m: float | None
    elapsed_s: float
    implied_speed_kmh: float
    road_speed_kmh: float | None
    plausible: bool
    #: True when a road distance was available for this pair.
    road_snapped: bool

    @property
    def distance_m(self) -> float:
        """Best available distance — the road where we have it."""
        return self.road_distance_m if self.road_distance_m is not None else self.direct_distance_m

    @property
    def note(self) -> str | None:
        if self.plausible:
            return None
        if self.elapsed_s <= 0:
            return (
                "Two cameras report this vehicle at the same instant. Either a "
                "cloned plate, a mis-read, or a camera clock that is wrong."
            )
        return (
            f"{self.implied_speed_kmh:.0f} km/h implied over "
            f"{self.direct_distance_m / 1000:.1f} km in a straight line — above the "
            f"{IMPLAUSIBLE_SPEED_KMH:.0f} km/h limit even by the shortest possible "
            "path. Treat as a possible cloned plate."
        )


@dataclass(frozen=True)
class Leg:
    """A run of visits with no implausible transition between them."""

    visits: list[Visit]

    @property
    def started(self) -> datetime:
        return self.visits[0].first_seen

    @property
    def ended(self) -> datetime:
        return self.visits[-1].last_seen


@dataclass(frozen=True)
class Journey:
    plate: str
    visits: list[Visit]
    hops: list[Hop]
    legs: list[Leg]
    confidence: float

    @property
    def implausible_hops(self) -> list[Hop]:
        return [h for h in self.hops if not h.plausible]

    @property
    def distance_m(self) -> float:
        return sum(h.distance_m for h in self.hops)

    @property
    def elapsed_s(self) -> float:
        if not self.visits:
            return 0.0
        return (self.visits[-1].last_seen - self.visits[0].first_seen).total_seconds()

    @property
    def summary(self) -> str:
        """One line an operator can read without decoding the JSON."""
        if not self.visits:
            return "No sightings."
        cameras = len({v.camera_id for v in self.visits})
        line = (
            f"{len(self.visits)} visits across {cameras} cameras, "
            f"{self.distance_m / 1000:.1f} km over {self.elapsed_s / 3600:.1f} h"
        )
        if self.implausible_hops:
            line += f" — {len(self.implausible_hops)} implausible transition(s)"
        return line


def collapse_revisits(rows: list[dict], window_s: float = REVISIT_WINDOW_S) -> list[Visit]:
    """Group consecutive same-camera sightings into visits.

    `rows` must be ordered by timestamp ascending and carry `camera_id`, `ts`,
    `lat`, `lon` and `confidence`. Rows without a position are skipped: a camera
    with no coordinate cannot be placed on a route, and inventing one would be
    worse than omitting it. The count of skipped rows is the caller's business —
    it is visible as the difference between sighting and visit totals.
    """
    visits: list[Visit] = []
    for row in rows:
        if row.get("lat") is None or row.get("lon") is None:
            continue

        ts = row["ts"]
        previous = visits[-1] if visits else None
        same_camera = previous is not None and previous.camera_id == row["camera_id"]
        within_window = (
            same_camera and (ts - previous.last_seen).total_seconds() <= window_s
        )

        if within_window:
            visits[-1] = Visit(
                camera_id=previous.camera_id,
                camera_name=previous.camera_name,
                district=previous.district,
                point=previous.point,
                first_seen=previous.first_seen,
                last_seen=max(previous.last_seen, ts),
                sightings=previous.sightings + 1,
                # The best read of the visit, not the mean: the vehicle was
                # either identified or it was not, and one clear frame settles
                # it. Averaging would let a run of poor reads bury a good one.
                confidence=max(previous.confidence, float(row["confidence"])),
                sighting_ids=[*previous.sighting_ids, row["id"]],
                thumbnail_url=previous.thumbnail_url or row.get("thumbnail_url"),
            )
            continue

        visits.append(
            Visit(
                camera_id=row["camera_id"],
                camera_name=row.get("camera_name"),
                district=row.get("district"),
                point=Point(float(row["lat"]), float(row["lon"])),
                first_seen=ts,
                last_seen=ts,
                sightings=1,
                confidence=float(row["confidence"]),
                sighting_ids=[row["id"]],
                thumbnail_url=row.get("thumbnail_url"),
            )
        )
    return visits


def reconstruct(
    plate: str,
    rows: list[dict],
    *,
    road_distances: dict[tuple[str, str], float] | None = None,
    window_s: float = REVISIT_WINDOW_S,
    limit_kmh: float = IMPLAUSIBLE_SPEED_KMH,
) -> Journey:
    """Build the movement history for one plate.

    `road_distances` maps a (from_camera, to_camera) pair to a road-network
    distance in metres. Any pair missing from it falls back to great-circle,
    flagged on the hop, so a routing engine that is down or that cannot reach a
    camera degrades one segment rather than failing the whole response — which
    is the mitigation named in the build plan's risk table.
    """
    visits = collapse_revisits(rows, window_s=window_s)
    road_distances = road_distances or {}

    hops: list[Hop] = []
    for previous, current in zip(visits, visits[1:], strict=False):
        key = (previous.camera_id, current.camera_id)
        road = road_distances.get(key)
        direct = haversine_m(previous.point, current.point)
        # Time is measured from when the first camera *stopped* seeing the
        # vehicle to when the next one first did. Using the first-seen times
        # would charge the dwell to the drive and understate the speed.
        elapsed = (current.first_seen - previous.last_seen).total_seconds()
        speed = implied_speed_kmh(direct, elapsed)
        road_speed = implied_speed_kmh(road, elapsed) if road is not None else None
        hops.append(
            Hop(
                from_camera=previous.camera_id,
                to_camera=current.camera_id,
                direct_distance_m=round(direct, 1),
                road_distance_m=round(road, 1) if road is not None else None,
                elapsed_s=round(elapsed, 1),
                implied_speed_kmh=round(speed, 1) if speed != float("inf") else float("inf"),
                road_speed_kmh=(
                    round(road_speed, 1)
                    if road_speed is not None and road_speed != float("inf")
                    else None
                ),
                # The direct distance, deliberately — see Hop's docstring.
                plausible=_plausible(direct, elapsed, limit_kmh),
                road_snapped=road is not None,
            )
        )

    return Journey(
        plate=plate,
        visits=visits,
        hops=hops,
        legs=_split_into_legs(visits, hops),
        confidence=_confidence(visits, hops),
    )


def _plausible(distance_m: float, elapsed_s: float, limit_kmh: float) -> bool:
    """A hop of no distance is plausible however fast it looks.

    Two consecutive visits to cameras metres apart — a junction covered twice —
    divide a tiny distance by a tiny time and produce a large speed that means
    nothing. Guarding on distance keeps the clone detector pointed at real
    movement instead of at surveying noise.
    """
    if distance_m < 1.0:
        return True
    from services.common.geo import is_plausible_transition

    return is_plausible_transition(distance_m, elapsed_s, limit_kmh=limit_kmh)


def _split_into_legs(visits: list[Visit], hops: list[Hop]) -> list[Leg]:
    """Cut the journey wherever a transition was not physically possible."""
    if not visits:
        return []
    legs: list[Leg] = []
    current = [visits[0]]
    for hop, visit in zip(hops, visits[1:], strict=False):
        if hop.plausible:
            current.append(visit)
        else:
            legs.append(Leg(visits=current))
            current = [visit]
    legs.append(Leg(visits=current))
    return legs


def _confidence(visits: list[Visit], hops: list[Hop]) -> float:
    """How much to believe this route.

    Two independent things can be wrong: the plate may have been misread, or the
    reads may be genuine but describe a path no single vehicle took. The first
    is the mean visit confidence; the second is a multiplicative penalty per
    implausible transition. Multiplicative because a second contradiction should
    compound the doubt without ever driving the figure below zero.
    """
    if not visits:
        return 0.0
    base = sum(v.confidence for v in visits) / len(visits)
    for hop in hops:
        if not hop.plausible:
            base *= IMPLAUSIBLE_PENALTY
    return round(min(max(base, 0.0), 1.0), 4)
