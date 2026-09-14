"""M4 acceptance test.

From docs/build-plan.md §5:

    Accept: entering a planted plate returns a route with >= 3 timestamped
            sightings in under 2 seconds, rendered on the map with a
            movement-history table.

M3 taught this file's shape. That milestone's acceptance passed 7/7 against a
`sightings` table in which every plate was a Python dataclass repr, because
every check counted rows and none of them looked at a value. So the checks here
interrogate the *content* of the journey: that the visits are ordered in time,
that they are on distinct cameras, that the hops imply speeds a car could
actually drive, and that the route geometry lands in Gujarat rather than in the
Indian Ocean.

The plausibility check is the one that would have caught the harness defect this
milestone opened with — a farm that reported one plate at two cameras 57 km
apart in the same second, which counted as a perfectly good three-sighting
journey until someone read the speeds.

Usage:
    python -m scripts.acceptance.m4
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEFAULT_API = "http://localhost:8000"
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

#: The plate planted along the cam-20..25 corridor. Not in the watchlist: the
#: trace path must be provable without alerting having fired, because the
#: evaluation hands over a registration number for a vehicle nobody was watching.
PLANTED_PLATE = "GJ18TR4321"

#: build-plan §5. This happens live in front of evaluators.
BUDGET_S = 2.0

#: build-plan §5 again — "a route with >= 3 timestamped sightings".
REQUIRED_SIGHTINGS = 3

#: Gujarat's bounding box. Catches the GeoJSON [lon, lat] transposition, which
#: otherwise yields a perfectly valid-looking route in the Indian Ocean.
GUJARAT_BBOX = (68.0, 20.0, 74.7, 24.8)

#: Nothing on a road drives faster than this. Above it the "journey" is the
#: harness teleporting a vehicle, which is how M4 opened.
MAX_PLAUSIBLE_KMH = 120.0


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def render(self) -> str:
        mark = f"{GREEN}PASS{RESET}" if self.passed else f"{RED}FAIL{RESET}"
        return f"  [{mark}] {self.name}\n         {DIM}{self.detail}{RESET}"


def _get(url: str, timeout: float = 30.0):
    req = urllib.request.Request(url)
    req.add_header("X-Actor", "acceptance-m4")
    req.add_header("X-Case-Ref", "ACC-M4")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read() or "null")


def _journey(api: str, plate: str) -> tuple[dict, float]:
    started = time.perf_counter()
    payload = _get(f"{api}/api/vehicles/{urllib.parse.quote(plate)}/journey")
    return payload, time.perf_counter() - started


def _visits(payload: dict) -> list[dict]:
    return [
        f["properties"] for f in payload.get("features", [])
        if f.get("properties", {}).get("kind") == "sighting"
    ]


def check_search_finds_the_plate(api: str, plate: str) -> Check:
    """The operator's entry point: type a plate, get reads back."""
    name = "Plate search finds the planted vehicle"
    spaced = f"{plate[:2]} {plate[2:4]} {plate[4:-4]} {plate[-4:]}"
    hits = _get(f"{api}/api/sightings/search?plate={urllib.parse.quote(spaced)}")
    cameras = {h["camera_id"] for h in hits}
    return Check(
        name,
        len(hits) > 0,
        f"typed as {spaced!r}, matched {len(hits)} sightings across {len(cameras)} "
        f"cameras — normalisation applied at query time as well as write time"
        if hits
        else f"no sightings for {plate}; has the corridor clip been seeded and played?",
    )


def check_journey_returns_a_route(api: str, plate: str, required: int) -> Check:
    name = f"Journey returns >= {required} timestamped sightings"
    payload, _ = _journey(api, plate)
    visits = _visits(payload)
    timestamped = [v for v in visits if v.get("ts")]
    props = payload.get("properties", {})
    return Check(
        name,
        len(timestamped) >= required,
        f"{len(timestamped)} visits across {props.get('cameras')} cameras, "
        f"{props.get('distance_m', 0) / 1000:.1f} km over "
        f"{props.get('elapsed_s', 0) / 60:.1f} min; confidence {props.get('confidence')}"
        if len(timestamped) >= required
        else f"only {len(timestamped)} timestamped visits (from "
             f"{props.get('sightings', 0)} raw sightings)",
    )


def check_within_budget(api: str, plate: str, budget: float) -> Check:
    """Measured over repeats: the demo will not be run on a warm cache by luck."""
    name = f"Journey query under {budget:.0f}s"
    timings = []
    for _ in range(5):
        payload, elapsed = _journey(api, plate)
        timings.append(elapsed)
    worst = max(timings)
    server = payload.get("properties", {}).get("query_ms")
    return Check(
        name,
        worst < budget,
        f"5 runs, worst {worst * 1000:.0f} ms, median "
        f"{sorted(timings)[2] * 1000:.0f} ms (server-side {server} ms)",
    )


def check_visits_are_a_real_journey(api: str, plate: str) -> Check:
    """Ordered in time, across distinct cameras, at drivable speeds.

    This is the check that would have caught the harness defect: a farm
    reporting one plate at two cameras 57 km apart in the same second produced
    a journey with plenty of sightings and no possible physical meaning.
    """
    name = "The route is a journey a vehicle could have made"
    payload, _ = _journey(api, plate)
    visits = _visits(payload)
    segments = payload.get("properties", {}).get("segments", [])

    if len(visits) < 2:
        return Check(name, False, "fewer than two visits; nothing to verify")

    times = [v["ts"] for v in visits]
    ordered = times == sorted(times)
    cameras = [v["camera_id"] for v in visits]
    distinct = len(set(cameras)) >= REQUIRED_SIGHTINGS

    speeds = [s["implied_speed_kmh"] for s in segments if s.get("implied_speed_kmh")]
    plausible = [s for s in segments if s.get("plausible")]
    fastest = max(speeds) if speeds else 0.0

    ok = ordered and distinct and len(plausible) >= REQUIRED_SIGHTINGS - 1
    return Check(
        name,
        ok,
        f"{len(visits)} visits in time order across {len(set(cameras))} distinct "
        f"cameras; {len(plausible)}/{len(segments)} hops plausible, fastest "
        f"{fastest:.0f} km/h (limit {MAX_PLAUSIBLE_KMH:.0f})"
        if ok
        else f"ordered={ordered}, distinct cameras={len(set(cameras))}, "
             f"plausible hops={len(plausible)}/{len(segments)}, fastest {fastest:.0f} km/h",
    )


def check_geometry_is_in_gujarat(api: str, plate: str) -> Check:
    """Guards the [lon, lat] transposition, and that a route line exists."""
    name = "Route geometry is GeoJSON and lands in Gujarat"
    payload, _ = _journey(api, plate)
    lines = [
        f for f in payload.get("features", [])
        if f.get("geometry", {}).get("type") == "LineString"
    ]
    if not lines:
        return Check(name, False, "no LineString feature; the map has nothing to draw")

    coords = lines[0]["geometry"]["coordinates"]
    west, south, east, north = GUJARAT_BBOX
    outside = [c for c in coords if not (west <= c[0] <= east and south <= c[1] <= north)]
    snapped = lines[0]["properties"].get("road_snapped")

    return Check(
        name,
        not outside,
        f"{len(coords)} positions, all inside Gujarat; "
        f"road_snapped={snapped}"
        + ("" if snapped else f" ({lines[0]['properties'].get('fallback_reason')})")
        if not outside
        else f"{len(outside)} of {len(coords)} positions outside Gujarat — "
             "likely a [lon, lat] transposition",
    )


def check_movement_history_table(api: str, plate: str) -> Check:
    """The response must carry everything the history table renders."""
    name = "Movement history is exportable from one response"
    payload, _ = _journey(api, plate)
    visits = _visits(payload)
    if not visits:
        return Check(name, False, "no visits")

    required = {"ts", "camera_name", "confidence", "sequence", "thumbnail_url", "dwell_s"}
    missing = {k for k in required if any(v.get(k) is None for v in visits)}
    segments = payload.get("properties", {}).get("segments", [])
    seg_fields = {"distance_m", "elapsed_s", "plausible", "road_snapped"}
    seg_missing = {
        k for k in seg_fields if any(s.get(k) is None for s in segments)
    } if segments else set()

    ok = not missing and not seg_missing
    return Check(
        name,
        ok,
        f"every visit carries {sorted(required)}; every hop carries {sorted(seg_fields)}"
        if ok
        else f"visits missing {sorted(missing)}; segments missing {sorted(seg_missing)}",
    )


def check_trace_is_audited(api: str, plate: str) -> Check:
    """A trace that leaves no trail cannot be evidence."""
    name = "Journey query is audited with its case reference"
    _journey(api, plate)
    entries = _get(f"{api}/api/audit?action=journey.query&limit=20")
    rows = entries if isinstance(entries, list) else entries.get("items", [])
    mine = [e for e in rows if e.get("case_ref") == "ACC-M4"]
    return Check(
        name,
        bool(mine),
        f"{len(mine)} journey.query entries bound to case ACC-M4, actor "
        f"{mine[0].get('actor')!r}"
        if mine
        else f"no audited journey.query with a case ref (saw {len(rows)} entries)",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="M4 acceptance test.")
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--plate", default=PLANTED_PLATE)
    parser.add_argument("--budget", type=float, default=BUDGET_S)
    parser.add_argument("--sightings", type=int, default=REQUIRED_SIGHTINGS)
    args = parser.parse_args()

    print(f"\n{'=' * 70}\nM4 acceptance — search and journey reconstruction\n{'=' * 70}\n")
    print(f"{DIM}Planted plate: {args.plate}{RESET}\n")

    checks = [
        check_search_finds_the_plate(args.api, args.plate),
        check_journey_returns_a_route(args.api, args.plate, args.sightings),
        check_within_budget(args.api, args.plate, args.budget),
        check_visits_are_a_real_journey(args.api, args.plate),
        check_geometry_is_in_gujarat(args.api, args.plate),
        check_movement_history_table(args.api, args.plate),
        check_trace_is_audited(args.api, args.plate),
    ]

    for check in checks:
        print(check.render())

    passed = sum(c.passed for c in checks)
    ok = passed == len(checks)
    print(f"\n{'=' * 70}")
    print(
        f"{GREEN}M4 ACCEPTANCE PASSED{RESET}" if ok
        else f"{RED}M4 ACCEPTANCE FAILED{RESET} — {passed}/{len(checks)} checks passed"
    )
    print(f"{'=' * 70}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
