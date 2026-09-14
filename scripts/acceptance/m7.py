"""M7 acceptance test.

From docs/build-plan.md §5:

    Accept: the page renders live figures under 50-stream load and is
            screen-recordable.

"Live" and "under load" are the two claims, and both are checkable without a
browser: the figures must move between two samples taken seconds apart, and the
stream count behind them must be at or above the 50 the milestone specifies. The
page rendering those figures is verified in the browser and the screenshot kept
in `docs/` — that half cannot be asserted from here, and pretending otherwise
would be the kind of check that passes while the thing is broken.

The last check is the one worth having. A performance page is exactly where a
platform is tempted to report the flattering version of itself, so this asserts
that the *uncomfortable* figures are on the surface too: the shed rate, the
scope of the accuracy claim, and whether writes are actually landing.

Usage:
    python -m scripts.acceptance.m7
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from dataclasses import dataclass

DEFAULT_API = "http://localhost:8000"
DEFAULT_WEB = "http://localhost:5173"
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

#: build-plan §5 for M3 and M7 both: fifty streams is the bar.
REQUIRED_STREAMS = 50

#: The figures build-plan §5 names for this page, as keys of /api/performance.
#: `queue depth` is deliberately absent — the pipeline sheds rather than queues,
#: and the page says so rather than printing a zero that would imply a queue
#: exists and is empty.
REQUIRED_FIGURES = (
    "cameras_processing", "mean_decode_fps", "sightings_per_minute",
    "sightings_indexed", "uptime_s",
)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def render(self) -> str:
        mark = f"{GREEN}PASS{RESET}" if self.passed else f"{RED}FAIL{RESET}"
        return f"  [{mark}] {self.name}\n         {DIM}{self.detail}{RESET}"


def _get(url: str, timeout: float = 60.0):
    req = urllib.request.Request(url)
    req.add_header("X-Actor", "acceptance-m7")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read() or "null")


def check_under_load(api: str) -> Check:
    name = f"Figures are reported under >= {REQUIRED_STREAMS}-stream load"
    perf = _get(f"{api}/api/performance?minutes=5")
    streams = perf.get("cameras_processing") or 0
    return Check(
        name, streams >= REQUIRED_STREAMS,
        f"{streams} cameras decoding concurrently, {perf.get('frames_decoded'):,} "
        f"frames in the window at a mean {perf.get('mean_decode_fps')} fps"
        if streams >= REQUIRED_STREAMS
        else f"only {streams} cameras processing; is the ANPR tier running?",
    )


def check_every_named_figure_is_present(api: str) -> Check:
    name = "Every figure the build plan names is measured"
    perf = _get(f"{api}/api/performance?minutes=15")
    missing = [k for k in REQUIRED_FIGURES if perf.get(k) in (None, "")]
    latency = perf.get("api_latency") or []
    if not latency:
        missing.append("api_latency (p95 query latency)")
    return Check(
        name, not missing,
        "streams active, fps, detections/min, sightings indexed, uptime and "
        f"p95 query latency over {len(latency)} routes"
        if not missing else f"missing or unmeasured: {', '.join(missing)}",
    )


def check_figures_are_live(api: str, gap_s: float) -> Check:
    """A static page of numbers is a screenshot, not evidence."""
    name = "The figures move — the page is live, not a snapshot"
    first = _get(f"{api}/api/performance?minutes=5")
    time.sleep(gap_s)
    second = _get(f"{api}/api/performance?minutes=5")

    moved = [
        key for key in ("frames_decoded", "uptime_s")
        if (second.get(key) or 0) > (first.get(key) or 0)
    ]
    return Check(
        name, len(moved) == 2,
        f"over {gap_s:.0f}s: frames decoded "
        f"{first.get('frames_decoded'):,} → {second.get('frames_decoded'):,}, "
        f"uptime advancing"
        if len(moved) == 2 else f"only {moved} changed in {gap_s:.0f}s",
    )


def check_query_latency_is_real(api: str) -> Check:
    """p95 over routes that were genuinely exercised, not a synthetic ping."""
    name = "p95 query latency is measured per route"
    perf = _get(f"{api}/api/performance?minutes=15")
    routes = {r["route"]: r for r in perf.get("api_latency", [])}
    journey = next((r for k, r in routes.items() if "journey" in k), None)
    if journey is None:
        return Check(name, False, "the journey route has not been exercised yet")
    return Check(
        name, journey["p95_ms"] > 0 and journey["requests"] > 0,
        f"{len(routes)} routes instrumented; the journey query — the one that "
        f"happens live in front of evaluators — is p50 {journey['p50_ms']} ms, "
        f"p95 {journey['p95_ms']} ms over {journey['requests']} requests",
    )


def check_the_page_is_served(web: str) -> Check:
    """The route exists and the bundle loads. Rendering is verified in a browser."""
    name = "The performance page is served and screen-recordable"
    try:
        with urllib.request.urlopen(f"{web}/", timeout=20) as resp:  # noqa: S310
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return Check(name, False, f"web app unreachable at {web}: {exc}")

    ok = "<div id=\"root\"" in html or "<div id='root'" in html
    return Check(
        name, ok,
        f"served at {web}/#/performance — a hash route, so it has a stable URL "
        "to record and link. Rendering verified in the browser; screenshot at "
        "docs/m7-performance.png",
    )


def check_the_uncomfortable_figures_are_shown(api: str) -> Check:
    """The check that makes the rest of the page trustworthy.

    A performance surface is exactly where a platform is tempted to show only
    what flatters it. Three figures here are unflattering and all three must be
    present: how much work is being shed, whether writes are landing, and how
    many cameras the accuracy claim is actually true of.
    """
    name = "The unflattering figures are on the page too"
    perf = _get(f"{api}/api/performance?minutes=15")
    capability = _get(f"{api}/api/cameras/anpr-capability?days=1")

    shedding = perf.get("load_shedding") or {}
    health = perf.get("write_health") or {}
    capable = (capability.get("summary") or {}).get("anpr_grade")

    missing = []
    if "ocr_shed_fraction" not in shedding:
        missing.append("OCR shed rate")
    if "healthy" not in health:
        missing.append("index write health")
    if capable is None:
        missing.append("ANPR-capable camera count")

    return Check(
        name, not missing,
        f"shed rate {shedding.get('ocr_shed_fraction', 0) * 100:.1f}%, writes "
        f"{'healthy' if health.get('healthy') else 'FAILING'}, accuracy scoped to "
        f"{capable} of {capability.get('cameras_assessed')} cameras"
        if not missing else f"missing: {', '.join(missing)}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="M7 acceptance test.")
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--web", default=DEFAULT_WEB)
    parser.add_argument("--gap", type=float, default=12.0, help="Seconds between samples.")
    args = parser.parse_args()

    print(f"\n{'=' * 70}\nM7 acceptance — end-to-end performance evidence\n{'=' * 70}\n")

    checks = [
        check_under_load(args.api),
        check_every_named_figure_is_present(args.api),
        check_figures_are_live(args.api, args.gap),
        check_query_latency_is_real(args.api),
        check_the_page_is_served(args.web),
        check_the_uncomfortable_figures_are_shown(args.api),
    ]

    for check in checks:
        print(check.render())

    passed = sum(c.passed for c in checks)
    ok = passed == len(checks)
    print(f"\n{'=' * 70}")
    print(
        f"{GREEN}M7 ACCEPTANCE PASSED{RESET}" if ok
        else f"{RED}M7 ACCEPTANCE FAILED{RESET} — {passed}/{len(checks)} checks passed"
    )
    print(f"{'=' * 70}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
