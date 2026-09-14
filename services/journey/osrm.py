"""Road-network distances and route geometry, with a fallback that always works.

The build plan names this as a risk: *"OSRM route snapping fails where cameras
are far from mapped roads — fall back to great-circle with a flag on the
segment; never fail the whole journey response."* That is the design here, and
it is applied at three levels rather than one:

- the routing service is unreachable → every hop falls back, the journey still
  renders, and `road_snapped` is false on each segment;
- the service answers but cannot route this particular pair → that hop falls
  back and the rest stay snapped;
- the service is slow → the request is bounded by a short timeout, because a
  journey query has a 2-second budget in front of evaluators and a hanging
  route call would spend all of it.

A trace that draws straight lines is worth far more than one that returns 503.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from services.common.config import settings
from services.common.geo import Point

log = logging.getLogger("journey.osrm")

__all__ = ["RouteResult", "route", "available"]

#: A journey query has a ~2 s budget end to end, and routing is one part of it.
#: Better a straight line quickly than a perfect road slowly.
TIMEOUT_S = 1.5

#: OSRM rejects very long coordinate lists in a GET. Our journeys are a handful
#: of cameras, so this only guards against a pathological query.
MAX_WAYPOINTS = 100


class RouteResult:
    """What the router returned, or an honest account of why it did not."""

    def __init__(
        self,
        geometry: list[Point],
        leg_distances_m: list[float],
        snapped: bool,
        reason: str | None = None,
    ) -> None:
        self.geometry = geometry
        self.leg_distances_m = leg_distances_m
        self.snapped = snapped
        self.reason = reason


def available() -> bool:
    """Whether the routing service answers at all. Used by the status surface."""
    try:
        with urllib.request.urlopen(  # noqa: S310 - configured internal URL
            f"{settings.osrm_url}/route/v1/driving/72.5,23.0;72.6,23.1?overview=false",
            timeout=TIMEOUT_S,
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def route(points: list[Point]) -> RouteResult:
    """Snap a sequence of camera positions to the road network.

    Returns the driven geometry and the per-leg road distances. On any failure
    the caller gets a straight-line geometry with `snapped=False` and a reason,
    never an exception: this function sits directly under the endpoint that has
    to work in front of evaluators.
    """
    if len(points) < 2:
        return RouteResult(geometry=list(points), leg_distances_m=[], snapped=False,
                           reason="not enough positions to route")
    if len(points) > MAX_WAYPOINTS:
        return RouteResult(geometry=list(points), leg_distances_m=[], snapped=False,
                           reason=f"more than {MAX_WAYPOINTS} waypoints")

    coords = ";".join(f"{p.lon:.6f},{p.lat:.6f}" for p in points)
    query = urllib.parse.urlencode({"overview": "full", "geometries": "geojson",
                                    "steps": "false", "annotations": "false"})
    url = f"{settings.osrm_url}/route/v1/driving/{coords}?{query}"

    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as resp:  # noqa: S310
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.warning("OSRM unavailable (%s); falling back to great-circle", exc)
        return RouteResult(list(points), [], snapped=False, reason=f"router unreachable: {exc}")

    if payload.get("code") != "Ok" or not payload.get("routes"):
        reason = payload.get("code", "unknown")
        log.info("OSRM could not route these points (%s)", reason)
        return RouteResult(list(points), [], snapped=False, reason=f"no route: {reason}")

    best = payload["routes"][0]
    geometry = [Point(lat, lon) for lon, lat in best.get("geometry", {}).get("coordinates", [])]
    legs = [float(leg.get("distance", 0.0)) for leg in best.get("legs", [])]

    # A leg per hop is the contract the caller relies on to key its distance
    # map. If OSRM returned a different number, the pairing would silently shift
    # and every hop after the first would be attributed the wrong distance —
    # so refuse the whole result rather than mis-align it.
    if len(legs) != len(points) - 1:
        log.warning("OSRM returned %d legs for %d waypoints; ignoring", len(legs), len(points))
        return RouteResult(list(points), [], snapped=False, reason="leg count mismatch")

    return RouteResult(geometry=geometry or list(points), leg_distances_m=legs, snapped=True)
