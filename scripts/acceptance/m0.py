"""M0 acceptance test.

From docs/build-plan.md §5:

    Accept: `docker compose up` gives 50 live RTSP endpoints, 50 registry rows,
            50 pins on the map.

Each of the three is checked against the running stack, not asserted. The map
pin count is checked by fetching the exact GeoJSON the map consumes — if that
FeatureCollection has 50 Point features, the map has 50 pins, and testing the
API rather than the browser keeps this runnable in CI.

Exit code 0 means M0 genuinely passes.

Usage:
    python -m scripts.acceptance.m0
    python -m scripts.acceptance.m0 --expect 50 --api http://localhost:8000
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

DEFAULT_API = "http://localhost:8000"
DEFAULT_MEDIAMTX_API = "http://localhost:9997"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def render(self) -> str:
        mark = f"{GREEN}PASS{RESET}" if self.passed else f"{RED}FAIL{RESET}"
        return f"  [{mark}] {self.name}\n         {DIM}{self.detail}{RESET}"


def _get_json(url: str, timeout: float = 10.0) -> object:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read())


def wait_for(url: str, timeout_s: float) -> bool:
    """Poll a URL until it answers or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            _get_json(url, timeout=3)
            return True
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            time.sleep(2)
    return False


def _farm(cams: list, prefix: str) -> list:
    """Just the simulated camera farm.

    M0 is "Foundation + 50-camera simulator", so its criteria are about that
    farm. The registry may also hold real government cameras — onboarding those
    is M1's job — and they legitimately lack surveyed bearing and field of view.
    Counting them here would either fail M0 for the wrong reason or force us to
    invent camera geometry, and invented geometry produces confidently wrong
    coverage polygons downstream.
    """
    return [c for c in cams if (c.get("external_ref") or "").startswith(prefix)]


def check_registry_rows(api: str, expect: int, prefix: str) -> Check:
    """Criterion 2: the registry holds the full farm."""
    try:
        all_cams = _get_json(f"{api}/api/cameras")
    except Exception as exc:  # noqa: BLE001
        return Check("Registry rows", False, f"{api}/api/cameras unreachable: {exc}")

    assert isinstance(all_cams, list)
    cams = _farm(all_cams, prefix)
    n = len(cams)
    with_geom = sum(1 for c in cams if c.get("lat") and c.get("lon"))
    with_geometry_fields = sum(
        1 for c in cams if c.get("bearing") is not None and c.get("fov_degrees") is not None
    )
    ok = n >= expect and with_geom == n and with_geometry_fields == n
    extra = len(all_cams) - n
    detail = (
        f"{n} simulated cameras registered (expected {expect}); {with_geom} with "
        f"coordinates, {with_geometry_fields} with bearing and field of view"
    )
    if extra:
        detail += f"; plus {extra} other cameras in the registry, not counted here"
    return Check("Registry rows", ok, detail)


def check_map_pins(api: str, expect: int, prefix: str) -> Check:
    """Criterion 3: the GeoJSON the map renders contains a pin per camera."""
    try:
        fc = _get_json(f"{api}/api/cameras/geojson")
    except Exception as exc:  # noqa: BLE001
        return Check("Map pins", False, f"{api}/api/cameras/geojson unreachable: {exc}")

    assert isinstance(fc, dict)
    feats = [
        f for f in fc.get("features", [])
        if (f.get("properties", {}).get("external_ref") or "").startswith(prefix)
    ]
    points = [
        f for f in feats
        if f.get("geometry", {}).get("type") == "Point"
        and len(f.get("geometry", {}).get("coordinates", [])) == 2
    ]
    # Gujarat's bounding box. Catches the classic lat/lon transposition, which
    # otherwise renders 50 perfectly valid pins in the Indian Ocean.
    in_gujarat = [
        f for f in points
        if 68.0 <= f["geometry"]["coordinates"][0] <= 75.0
        and 20.0 <= f["geometry"]["coordinates"][1] <= 25.0
    ]
    ok = len(points) >= expect and len(in_gujarat) == len(points)
    return Check(
        "Map pins",
        ok,
        f"{len(points)} GeoJSON Point features (expected {expect}); "
        f"{len(in_gujarat)} inside the Gujarat bounding box",
    )


def check_rtsp_endpoints(mediamtx_api: str, api: str, expect: int) -> Check:
    """Criterion 1: the streams are actually live, per the media server."""
    try:
        paths = _get_json(f"{mediamtx_api}/v3/paths/list?itemsPerPage=500")
    except Exception as exc:  # noqa: BLE001
        return Check("Live RTSP endpoints", False, f"MediaMTX API unreachable: {exc}")

    assert isinstance(paths, dict)
    items = paths.get("items", [])
    # "ready" means a publisher is connected and sending — a path can exist
    # without being live, and only live counts here.
    ready = [p for p in items if p.get("ready")]

    # Cross-check against the registry: the streams must be the ones the
    # registry named, not any 50 streams (invariant 4).
    expected_paths: set[str] = set()
    try:
        cams = _get_json(f"{api}/api/cameras")
        assert isinstance(cams, list)
        # Only RTSP cameras are published to our media server. A camera fetched
        # straight from an HTTP endpoint never appears here and its absence is
        # not a fault.
        expected_paths = {
            urlparse(c["stream_ref"]).path.lstrip("/")
            for c in cams
            if str(c.get("stream_ref", "")).startswith("rtsp")
        }
    except Exception:  # noqa: BLE001
        pass

    ready_names = {p.get("name") for p in ready}
    matched = expected_paths & ready_names if expected_paths else ready_names

    ok = len(ready) >= expect and (not expected_paths or len(matched) >= expect)
    detail = f"{len(ready)} of {len(items)} MediaMTX paths ready (expected {expect})"
    if expected_paths:
        detail += f"; {len(matched)} match a registry stream_ref"
        missing = sorted(expected_paths - ready_names)[:5]
        if missing:
            detail += f"; missing e.g. {', '.join(missing)}"
    return Check("Live RTSP endpoints", ok, detail)


def check_rtsp_reachable(host: str, port: int) -> Check:
    """The RTSP port answers — a TCP-level sanity check under the API check."""
    try:
        with socket.create_connection((host, port), timeout=5):
            return Check("RTSP port reachable", True, f"{host}:{port} accepting connections")
    except OSError as exc:
        return Check("RTSP port reachable", False, f"{host}:{port} refused: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="M0 acceptance test.")
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--mediamtx-api", default=DEFAULT_MEDIAMTX_API)
    parser.add_argument("--rtsp-host", default="localhost")
    parser.add_argument("--rtsp-port", type=int, default=8554)
    parser.add_argument("--expect", type=int, default=50, help="cameras expected")
    parser.add_argument(
        "--prefix", default="cam-",
        help="external_ref prefix identifying the simulated farm M0 is about",
    )
    parser.add_argument(
        "--wait", type=float, default=120.0,
        help="seconds to wait for the API before testing (0 to skip)",
    )
    args = parser.parse_args()

    print(f"\n{'=' * 68}\nM0 acceptance — foundation and camera farm\n{'=' * 68}\n")

    if args.wait > 0:
        print(f"  {DIM}waiting up to {args.wait:.0f}s for {args.api}/health ...{RESET}")
        if not wait_for(f"{args.api}/health", args.wait):
            print(f"\n  [{RED}FAIL{RESET}] API never became reachable at {args.api}")
            print(f"\n  {YELLOW}Is the stack up? Try: make up && make logs{RESET}\n")
            return 1

    checks = [
        check_registry_rows(args.api, args.expect, args.prefix),
        check_map_pins(args.api, args.expect, args.prefix),
        check_rtsp_reachable(args.rtsp_host, args.rtsp_port),
        check_rtsp_endpoints(args.mediamtx_api, args.api, args.expect),
    ]

    for c in checks:
        print(c.render())

    passed = all(c.passed for c in checks)
    print(f"\n{'=' * 68}")
    if passed:
        print(f"{GREEN}M0 ACCEPTANCE PASSED{RESET} — "
              f"{args.expect} live RTSP endpoints, {args.expect} registry rows, "
              f"{args.expect} map pins.")
    else:
        failed = [c.name for c in checks if not c.passed]
        print(f"{RED}M0 ACCEPTANCE FAILED{RESET} — {', '.join(failed)}")
    print(f"{'=' * 68}\n")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
