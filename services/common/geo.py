"""Geodesic helpers.

Used for camera placement now, and for journey plausibility in M4 — the rule
that an implied speed above ~120 km/h between consecutive sightings splits the
journey rather than being silently dropped, because that is how cloned plates
surface.
"""

from __future__ import annotations

import math
from typing import NamedTuple

__all__ = [
    "Point",
    "EARTH_RADIUS_M",
    "IMPLAUSIBLE_SPEED_KMH",
    "haversine_m",
    "initial_bearing_deg",
    "polyline_length_m",
    "interpolate_polyline",
    "implied_speed_kmh",
    "is_plausible_transition",
]

EARTH_RADIUS_M = 6_371_008.8

# Above this, the two sightings cannot be the same vehicle travelling normally.
# Gujarat expressway limits top out at 120 km/h; anything faster is a clone, a
# mis-read, or a clock problem — all of which are signal.
IMPLAUSIBLE_SPEED_KMH = 120.0


class Point(NamedTuple):
    """WGS84 coordinate. Note the order: lat, lon — GeoJSON is lon, lat."""

    lat: float
    lon: float

    def to_geojson(self) -> list[float]:
        """GeoJSON position: [lon, lat]. Conventions live here, not at call sites."""
        return [self.lon, self.lat]


def haversine_m(a: Point, b: Point) -> float:
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(a.lat), math.radians(b.lat)
    dphi = phi2 - phi1
    dlambda = math.radians(b.lon - a.lon)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def initial_bearing_deg(a: Point, b: Point) -> int:
    """Initial bearing from ``a`` to ``b``, degrees clockwise from true north.

    Rounded to an int because `cameras.bearing` is a SMALLINT constrained to
    0-359, and sub-degree precision on a pole-mounted camera is a fiction.
    """
    phi1, phi2 = math.radians(a.lat), math.radians(b.lat)
    dlambda = math.radians(b.lon - a.lon)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return int(round(math.degrees(math.atan2(y, x)))) % 360


def polyline_length_m(points: list[Point]) -> float:
    return sum(haversine_m(points[i], points[i + 1]) for i in range(len(points) - 1))


def interpolate_polyline(points: list[Point], count: int) -> list[Point]:
    """``count`` positions spaced equally *by distance* along the polyline.

    Equal spacing by distance, not by waypoint index — otherwise cameras bunch
    up wherever the corridor description happens to have dense waypoints.
    """
    if count < 1:
        return []
    if len(points) < 2:
        return [points[0]] * count if points else []

    segments = [haversine_m(points[i], points[i + 1]) for i in range(len(points) - 1)]
    total = sum(segments)
    if total == 0:
        return [points[0]] * count

    # Inset by half a step at each end so no camera sits exactly on a terminus.
    step = total / count
    targets = [step * (i + 0.5) for i in range(count)]

    out: list[Point] = []
    seg_idx = 0
    seg_start = 0.0
    for target in targets:
        while seg_idx < len(segments) - 1 and seg_start + segments[seg_idx] < target:
            seg_start += segments[seg_idx]
            seg_idx += 1
        frac = (target - seg_start) / segments[seg_idx] if segments[seg_idx] else 0.0
        a, b = points[seg_idx], points[seg_idx + 1]
        out.append(Point(a.lat + (b.lat - a.lat) * frac, a.lon + (b.lon - a.lon) * frac))
    return out


def implied_speed_kmh(distance_m: float, elapsed_s: float) -> float:
    """Speed implied by a hop. Zero elapsed time yields ``inf``, not a crash."""
    if elapsed_s <= 0:
        return math.inf
    return (distance_m / elapsed_s) * 3.6


def is_plausible_transition(
    distance_m: float, elapsed_s: float, limit_kmh: float = IMPLAUSIBLE_SPEED_KMH
) -> bool:
    """False when the hop is too fast to be one vehicle — flag it, never drop it.

    The limit is compared with a relative tolerance: a hop computing to
    120.00000000000001 km/h is a rounding artefact, and accusing a vehicle of
    being a clone on the strength of a float's last bit is a false positive we
    can trivially avoid.
    """
    speed = implied_speed_kmh(distance_m, elapsed_s)
    return speed <= limit_kmh or math.isclose(speed, limit_kmh, rel_tol=1e-9)
