"""Retrospective trace — the endpoint the test case turns on.

    GET /api/vehicles/{plate}/journey?from=&to=

One request returns everything the map, the timeline and the movement-history
export need. That is deliberate and is the build plan's instruction: under live
demo conditions, fewer round trips is fewer failure modes.

The response is a GeoJSON `FeatureCollection` because the GeoJSON convention requires
anything geographic to be GeoJSON rather than an ad-hoc lat/lng object, so the
map layer consumes it directly with no translation step to get wrong:

- one `LineString` — the route, road-snapped where OSRM could
- one `Point` per visit — with times, camera, confidence and thumbnail
- `properties.segments[]` — per-hop distance, elapsed, implied speed, plausible
- `properties.confidence` — overall, degraded by implausible transitions

The plate is normalised on the way in with the same positional rules used when
the sighting was written (invariant 3), so an operator typing `GJ 01 AB 1234`
finds a read stored as `GJO1AB1234`.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query

from services.common.audit import Action, record
from services.common.db import fetch_all
from services.common.geo import Point
from services.common.plates import normalise_plate, plate_key_variants
from services.journey import Journey, reconstruct
from services.journey.osrm import route as osrm_route

router = APIRouter(prefix="/api/vehicles", tags=["journey"])

#: Hard cap on sightings pulled for one journey. A plate parked in front of a
#: camera for a week is a real case and must not turn into an unbounded query
#: with a 2-second budget in front of evaluators. Ordered by time descending in
#: the query and re-sorted ascending here, so the cap keeps the *most recent*
#: history rather than an arbitrary slice.
MAX_SIGHTINGS = 5_000

_SELECT = """
SELECT s.id, s.ts, s.camera_id::text AS camera_id, c.name AS camera_name,
       c.district, ST_Y(c.geom::geometry) AS lat, ST_X(c.geom::geometry) AS lon,
       s.confidence, s.plate_normalised, s.plate_raw, s.format_valid,
       s.vehicle_class, s.condition, s.vehicle_uid, s.uid_via, s.uid_distance
  FROM sightings s
  JOIN cameras c ON c.id = s.camera_id
 WHERE s.plate_normalised = ANY(%(keys)s)
"""

#: `#1234` traces a vehicle id instead of a plate (services/anpr/linking.py):
#: the plate sightings and the appearance links that joined them to it.
_SELECT_UID = _SELECT.replace("s.plate_normalised = ANY(%(keys)s)", "s.vehicle_uid = %(uid)s")


def _parse_ts(value: str | None, field: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(422, f"{field} is not an ISO-8601 timestamp: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@router.get(
    "/{plate}/journey",
    summary="Reconstruct a vehicle's movement history",
    description=(
        "The retrospective trace. Returns a GeoJSON FeatureCollection with the "
        "route as a LineString, one Point per camera visit, and per-hop "
        "plausibility in `properties.segments`.\n\n"
        "Consecutive sightings on one camera are collapsed into a single visit "
        "with a dwell time — a vehicle held in view produces a new track every "
        "30 seconds, and rendering those separately would show it teleporting "
        "on the spot.\n\n"
        "A hop implying more than 120 km/h splits the journey into legs and "
        "lowers the overall confidence, but **no sighting is ever dropped**: an "
        "impossible transition is how a cloned plate surfaces.\n\n"
        "Road distances come from OSRM where it can route the pair, and fall "
        "back to great-circle per segment otherwise — flagged as "
        "`road_snapped: false` rather than failing the response.\n\n"
        "**`#1234` instead of a plate** follows a vehicle id: sightings linked by "
        "a readable plate or by a learned appearance match (`uid_via`, "
        "`uid_distance` on each visit). An appearance link is a lead to check "
        "against the crops, not an identification.\n\n"
        "Audited as `journey.query`. Pass `X-Case-Ref` to bind the trace to an "
        "investigation."
    ),
)
def vehicle_journey(
    plate: str,
    from_: Annotated[str | None, Query(alias="from", description="ISO-8601 start.")] = None,
    to: Annotated[str | None, Query(description="ISO-8601 end.")] = None,
    snap: Annotated[bool, Query(description="Snap to the road network via OSRM.")] = True,
    actor: Annotated[str, Header(alias="X-Actor")] = "anonymous",
    case_ref: Annotated[str | None, Header(alias="X-Case-Ref")] = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    uid = plate.strip().lstrip("#")
    by_uid = plate.strip().startswith("#") and uid.isdigit()
    key = f"#{uid}" if by_uid else normalise_plate(plate)
    variants = [] if by_uid else plate_key_variants(plate)

    record(
        Action.JOURNEY_QUERY, actor, subject=key, case_ref=case_ref,
        detail={"query": plate, "from": from_, "to": to, "snap": snap},
    )

    params: dict[str, Any] = {"keys": variants, "uid": int(uid) if by_uid else None,
                              "limit": MAX_SIGHTINGS}
    clauses = ""
    if start := _parse_ts(from_, "from"):
        clauses += " AND s.ts >= %(from)s"
        params["from"] = start
    if end := _parse_ts(to, "to"):
        clauses += " AND s.ts <= %(to)s"
        params["to"] = end

    rows = fetch_all(
        (_SELECT_UID if by_uid else _SELECT) + clauses + " ORDER BY s.ts DESC LIMIT %(limit)s",
        params,
    )
    rows.reverse()  # chronological, which is what a movement history means

    journey = reconstruct(key, rows)

    geometry: list[Point] = [v.point for v in journey.visits]
    snapped = False
    snap_reason = "not requested" if not snap else None

    if snap and len(journey.visits) >= 2:
        result = osrm_route(geometry)
        snapped = result.snapped
        snap_reason = result.reason
        if result.snapped:
            # Re-run with the road distances now known. Cheap — it is arithmetic
            # over a handful of visits — and it means the plausibility verdict is
            # made against the distance a vehicle would actually have driven,
            # not the straight line, which is the whole point of snapping.
            distances = {
                (a.camera_id, b.camera_id): d
                for a, b, d in zip(
                    journey.visits, journey.visits[1:], result.leg_distances_m, strict=False
                )
            }
            journey = reconstruct(key, rows, road_distances=distances)
            geometry = result.geometry

    took_ms = round((time.perf_counter() - started) * 1000, 1)
    return _feature_collection(
        journey, geometry, rows, snapped=snapped, snap_reason=snap_reason, took_ms=took_ms
    )


def _feature_collection(
    journey: Journey,
    geometry: list[Point],
    rows: list[dict],
    *,
    snapped: bool,
    snap_reason: str | None,
    took_ms: float,
) -> dict[str, Any]:
    features: list[dict[str, Any]] = []

    if len(geometry) >= 2:
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [p.to_geojson() for p in geometry],
            },
            "properties": {
                "kind": "route",
                "road_snapped": snapped,
                # Named when absent so a straight line on the map is explained
                # rather than mistaken for the road.
                "fallback_reason": None if snapped else snap_reason,
            },
        })

    last_index = len(journey.visits) - 1
    by_id = {r["id"]: r for r in rows}
    for index, visit in enumerate(journey.visits):
        linked = [by_id[i] for i in visit.sighting_ids if i in by_id]
        distances = [r["uid_distance"] for r in linked if r.get("uid_distance") is not None]
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": visit.point.to_geojson()},
            "properties": {
                "kind": "sighting",
                "sequence": index + 1,
                # The map renders direction by colour rather than by numbered
                # labels, because a symbol layer needs a glyphs URL the style
                # deliberately does not have.
                "is_last": index == last_index,
                "camera_id": visit.camera_id,
                "camera_name": visit.camera_name,
                "district": visit.district,
                "ts": visit.first_seen.isoformat(),
                "last_seen": visit.last_seen.isoformat(),
                "dwell_s": round(visit.dwell_s, 1),
                "sightings": visit.sightings,
                "sighting_ids": visit.sighting_ids,
                "confidence": round(visit.confidence, 4),
                "thumbnail_url": f"/api/sightings/{visit.sighting_ids[0]}/thumbnail",
                # How these sightings joined their vehicle id (linking.py). A
                # visit reached only by `appearance` is a lead, not a plate read.
                "vehicle_uids": sorted({r["vehicle_uid"] for r in linked if r.get("vehicle_uid")}),
                "uid_via": sorted({r["uid_via"] for r in linked if r.get("uid_via")}),
                "uid_distance": min(distances) if distances else None,
            },
        })

    segments = [
        {
            "from_camera": hop.from_camera,
            "to_camera": hop.to_camera,
            "distance_m": hop.distance_m,
            # Both distances, because they disagree by up to 2x on this estate
            # and only one of them is safe to judge by. See journey.Hop.
            "direct_distance_m": hop.direct_distance_m,
            "road_distance_m": hop.road_distance_m,
            "elapsed_s": hop.elapsed_s,
            "implied_speed_kmh": (
                None if hop.implied_speed_kmh == float("inf") else hop.implied_speed_kmh
            ),
            "road_speed_kmh": hop.road_speed_kmh,
            "plausible": hop.plausible,
            "plausibility_basis": "direct",
            "road_snapped": hop.road_snapped,
            "note": hop.note,
        }
        for hop in journey.hops
    ]

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "plate": journey.plate,
            "confidence": journey.confidence,
            "summary": journey.summary,
            "sightings": len(rows),
            "visits": len(journey.visits),
            "cameras": len({v.camera_id for v in journey.visits}),
            "distance_m": round(journey.distance_m, 1),
            "elapsed_s": round(journey.elapsed_s, 1),
            "first_seen": journey.visits[0].first_seen.isoformat() if journey.visits else None,
            "last_seen": journey.visits[-1].last_seen.isoformat() if journey.visits else None,
            "road_snapped": snapped,
            "legs": [
                {
                    "sequence": i + 1,
                    "cameras": [v.camera_id for v in leg.visits],
                    "started": leg.started.isoformat(),
                    "ended": leg.ended.isoformat(),
                }
                for i, leg in enumerate(journey.legs)
            ],
            "implausible_transitions": len(journey.implausible_hops),
            "segments": segments,
            "query_ms": took_ms,
        },
    }
