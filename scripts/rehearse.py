"""Watch the stack until each demo shot is actually ready to film.

For recording the demonstration video. The video is a hard gate, since criterion
04 disqualifies anything that looks like a mock-up, and the two moments it turns
on are both timed by the harness rather than by the operator:

  - the planted alert GJ05UV9972 fires roughly 90 seconds into a run, because
    the vehicle has to come round again;
  - the planted trace GJ18TR4321 reads as one clean journey only inside about
    one cycle of the 25-minute corridor clip.

Waiting for either on camera is dead air, and guessing wrong is worse. Measured
on the hosted instance on 6 September, an unbounded trace of the planted plate
returned 260 visits, 66 legs, 65 implausible transitions and 0% confidence,
because the clip had looped for days and the clone detector was correctly
refusing to call that one vehicle. A judge watching that fill the panel reads
failure, not care.

So this polls the running platform and says, for each shot, ready or not yet and
why. It changes nothing: no seeding, no deletion, no writes. It is a readiness
probe, and the reason it is not a seeding script is invariant 1. Every plate read
is kept, so the fix for a polluted trace is to narrow the window, never to delete
sightings.

Usage::

    python -m scripts.rehearse                 # one pass, print the board
    python -m scripts.rehearse --watch         # poll until every shot is ready
    python -m scripts.rehearse --api https://host --token "$TOKEN"
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

log = logging.getLogger("rehearse")

GREEN, RED, AMBER, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m",
)

#: The planted trace vehicle. Deliberately **not** in the watchlist: the trace
#: path has to be provable without an alert having fired, because the evaluation
#: hands over a registration number for a vehicle nobody was watching.
TRACE_PLATE = "GJ18TR4321"

#: The planted alerting vehicle. In the watchlist, and the payoff of the video.
ALERT_PLATE = "GJ05UV9972"

#: The trace window, and it is bounded from both sides.
#:
#: `scripts/seed.py:JOURNEY_CLIP` is 25 minutes long, and the corridor's seeded
#: offsets stagger the six cameras by 238 s each — so the planted vehicle takes
#: **~19.5 minutes** to travel cam-20 to cam-25, and the whole thing repeats
#: every **25 minutes**. Measured 7 Sep: cam-20 at 19:44:43, cam-25 ~20:04.
#:
#: Too narrow and the window holds part of a journey. Too wide and it holds
#: cam-20's *next* pass alongside cam-25's current one — two appearances of one
#: vehicle 25 minutes apart at opposite ends of the corridor, which the
#: plausibility check correctly reports as impossible. 22 minutes sits between
#: the traverse and the cycle.
#:
#: There is still only a window of a few minutes each cycle when a query returns
#: exactly one clean pass, which is the entire reason this script exists rather
#: than a note saying "use 22 minutes".
CLIP_CYCLE_MINUTES = 22

#: What the shot needs to look like. Three cameras is the build-plan acceptance
#: figure; zero implausible hops is what makes the confidence number worth
#: showing.
MIN_VISITS = 3
MIN_CAMERAS = 3

#: docs/build-plan §5. This happens live in front of evaluators.
JOURNEY_BUDGET_S = 2.0

#: How much recent history the performance figures are taken over.
#:
#: `/api/performance` defaults to 15 minutes, which is right for a dashboard and
#: wrong for a readiness board: after a configuration change the window stays
#: full of the old regime, so the board reports a problem that was fixed ten
#: minutes ago and an operator either waits for nothing or "fixes" it twice.
#: Observed exactly that — shed read 94% at 15 minutes and **0%** at 5, with the
#: same stack, because the wider window still contained an estate of 50 cameras
#: that had already been scoped down to 9.
PERFORMANCE_WINDOW_MINUTES = 5

#: How long an alert stays interesting to point a camera at.
ALERT_FRESH_MINUTES = 15


@dataclass(frozen=True)
class Shot:
    """One thing that has to be true before a segment can be filmed."""

    name: str
    ready: bool
    detail: str
    #: What to do about it, when it is not ready. Empty when it is.
    advice: str = ""

    def render(self) -> str:
        mark = f"{GREEN}READY{RESET}" if self.ready else f"{AMBER}WAIT {RESET}"
        line = f"  {mark}  {self.name:<34} {self.detail}"
        if not self.ready and self.advice:
            line += f"\n         {DIM}{self.advice}{RESET}"
        return line


def _get(api: str, path: str, token: str | None, *, timeout: float = 30.0):
    request = urllib.request.Request(api.rstrip("/") + path)
    request.add_header("X-Actor", "rehearsal")
    # A trace is audited, and an audit row with no case reference is the thing
    # the DPDP position says never happens. Rehearsals get their own.
    request.add_header("X-Case-Ref", "REHEARSAL")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _journey(api: str, plate: str, token: str | None, minutes: int | None):
    query = ""
    if minutes:
        since = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()
        query = "?" + urllib.parse.urlencode({"from": since})
    started = time.monotonic()
    payload = _get(api, f"/api/vehicles/{urllib.parse.quote(plate)}/journey{query}", token)
    return payload, time.monotonic() - started


# --------------------------------------------------------------------------
# The shots
# --------------------------------------------------------------------------


def check_estate(api: str, token: str | None) -> Shot:
    """Cameras online and sightings climbing — the background of every shot."""
    try:
        cameras = _get(api, "/api/cameras?format=geojson", token)
    except Exception as exc:  # noqa: BLE001 - any failure is "not ready"
        return Shot("Estate up", False, f"{RED}{type(exc).__name__}: {exc}{RESET}",
                    "is the stack running? docker compose ps")
    features = cameras.get("features", cameras) if isinstance(cameras, dict) else cameras
    total = len(features)
    online = sum(
        1 for f in features
        if (f.get("properties", f) or {}).get("status") == "online"
    )
    ok = online >= MIN_CAMERAS
    return Shot("Estate up", ok, f"{online} of {total} cameras online",
                "" if ok else "wait for the health prober, or make sim")


def check_trace(api: str, token: str | None) -> tuple[Shot, dict]:
    """The journey shot. This is the one the loop pollutes."""
    try:
        windowed, elapsed = _journey(api, TRACE_PLATE, token, CLIP_CYCLE_MINUTES)
    except Exception as exc:  # noqa: BLE001
        return Shot("Trace — journey", False, f"{RED}{exc}{RESET}",
                    "check the API is reachable and the token is valid"), {}

    props = windowed.get("properties", {})
    visits = props.get("visits", 0)
    cameras = props.get("cameras", 0)
    implausible = props.get("implausible_transitions", 0)
    confidence = props.get("confidence", 0)

    detail = (
        f"{visits} visits / {cameras} cameras, {implausible} implausible, "
        f"confidence {confidence:.0%}, {elapsed * 1000:.0f} ms "
        f"{DIM}(last {CLIP_CYCLE_MINUTES} min){RESET}"
    )
    ready = (
        visits >= MIN_VISITS
        and cameras >= MIN_CAMERAS
        and implausible == 0
        and elapsed <= JOURNEY_BUDGET_S
    )
    if ready:
        advice = ""
    elif visits == 0:
        advice = (
            f"no reads yet in this cycle — the corridor clip is "
            f"{CLIP_CYCLE_MINUTES} min and the pass has not come round. Wait."
        )
    elif implausible:
        advice = (
            "the window has caught more than one pass of the same clip. Narrow "
            "it, or wait for the next cycle to start cleanly."
        )
    elif visits < MIN_VISITS:
        advice = (
            f"only {visits} of the corridor's cameras read it this cycle — OCR "
            "shed the rest. See the shed rate above; the fix is more shards, "
            "not fewer."
        )
    else:
        advice = f"query took {elapsed:.2f}s against a {JOURNEY_BUDGET_S}s budget"
    return Shot("Trace — journey", ready, detail, advice), props


def check_trace_is_not_filmed_unbounded(api: str, token: str | None) -> Shot:
    """Say plainly what an all-time trace looks like right now.

    Not a gate — it is *allowed* to be ugly, and on a long-running instance it
    will be. It is here so the number is on screen before filming rather than
    discovered afterwards, and so the operator picks the window deliberately.
    """
    try:
        payload, _ = _journey(api, TRACE_PLATE, token, None)
    except Exception as exc:  # noqa: BLE001
        return Shot("Trace — all-time (context)", True, f"{DIM}unavailable: {exc}{RESET}")
    props = payload.get("properties", {})
    implausible = props.get("implausible_transitions", 0)
    detail = (
        f"{props.get('visits', 0)} visits, {implausible} implausible, "
        f"confidence {props.get('confidence', 0):.0%}"
    )
    if implausible:
        detail += f"  {AMBER}<- do not film this view{RESET}"
    return Shot("Trace — all-time (context)", True, detail,
                "" if not implausible else
                f"set the panel's window to {CLIP_CYCLE_MINUTES} min before recording")


def check_alert(api: str, token: str | None) -> Shot:
    """The payoff shot: a watchlist hit with an evidence card."""
    try:
        payload = _get(api, "/api/alerts?limit=50", token)
    except Exception as exc:  # noqa: BLE001
        return Shot("Alert — watchlist hit", False, f"{RED}{exc}{RESET}", "")
    alerts = payload.get("alerts", payload) if isinstance(payload, dict) else payload
    planted = [a for a in alerts if (a.get("plate") or "").upper() == ALERT_PLATE]
    if not planted:
        return Shot("Alert — watchlist hit", False, f"no {ALERT_PLATE} alert yet",
                    "it fires ~90s into a run once the vehicle comes round; "
                    "check the plate is on the watchlist (make seed)")

    # `raised_at` is the field the API actually returns. The first version of
    # this looked for `ts`/`created_at`, found neither, left the age as None and
    # so reported every alert as stale — a check that could never pass, which is
    # worse than no check because it looks like a real finding.
    newest = max(planted, key=lambda a: a.get("raised_at") or "")
    stamp = newest.get("raised_at")
    age_min = None
    if stamp:
        try:
            age_min = (datetime.now(UTC) - datetime.fromisoformat(stamp)).total_seconds() / 60
        except ValueError:
            age_min = None
    fresh = age_min is not None and age_min <= ALERT_FRESH_MINUTES
    detail = f"{len(planted)} alerts, newest {newest.get('tier', '?')}"
    if age_min is not None:
        detail += f", {age_min:.0f} min old"
    return Shot("Alert — watchlist hit", fresh, detail,
                "" if fresh else
                f"newest is older than {ALERT_FRESH_MINUTES} min — it will scroll "
                "as stale on camera; wait for the next pass")


def check_backend_proof(api: str, token: str | None) -> Shot:
    """The 20 seconds that defeat the mock-up disqualifier."""
    try:
        perf = _get(api, f"/api/performance?minutes={PERFORMANCE_WINDOW_MINUTES}", token)
    except Exception as exc:  # noqa: BLE001
        return Shot("Backend proof cut", False, f"{RED}{exc}{RESET}", "")
    anpr = perf.get("anpr", perf)
    written = anpr.get("sightings_written") or perf.get("sightings_written") or 0
    cameras = anpr.get("cameras_processing") or perf.get("cameras_processing") or 0
    ready = bool(written) and bool(cameras)
    return Shot("Backend proof cut", ready,
                f"{written} sightings written, {cameras} cameras processing",
                "" if ready else "/api/performance has nothing to show yet")


#: Above this, OCR is being dropped faster than the demo can survive. The
#: platform sheds rather than queues, by design — but a shed read weakens a
#: vehicle's per-track vote, and a shed *enough* read means no plate at all and
#: an attribute-only row instead. Measured history: ~6% shed at 57 cameras with
#: six shards, 94.7% at 76 cameras, and 99.8% at 50 cameras on **two** shards,
#: which is the state that produced an empty trace and a plateless report.
MAX_SHED_FRACTION = 0.60

#: Suppressions that outnumber the reads kept are the overlay filter eating
#: plates, not a camera's clock being ignored. Measured 7-14 Sep 2026: 1,154
#: suppressed against 0 kept in five minutes, a week of no plates, and a 0% shed
#: rate that made this line green. The floor stops a quiet window tripping it.
MIN_SUPPRESSED_TO_JUDGE = 20


def check_ocr_keeping_up(api: str, token: str | None) -> Shot:
    """The shed rate, which is upstream of almost every other failure here.

    Worth its own line because the symptoms are all somewhere else: the journey
    has no visits, the report has no plates, the alert never re-fires. Each of
    those reads as a different problem, and all three are this one.
    """
    try:
        perf = _get(api, f"/api/performance?minutes={PERFORMANCE_WINDOW_MINUTES}", token)
    except Exception as exc:  # noqa: BLE001
        return Shot("OCR keeping up", False, f"{RED}{exc}{RESET}", "")
    anpr = perf.get("anpr", perf)
    shed = (anpr.get("load_shedding") or perf.get("load_shedding") or {})
    suppressed = shed.get("overlay_suppressed") or 0
    kept = anpr.get("plate_reads") or perf.get("plate_reads") or 0
    if suppressed >= MIN_SUPPRESSED_TO_JUDGE and suppressed > kept:
        return Shot(
            "OCR keeping up", False,
            f"{suppressed} reads suppressed as burnt-in text, {kept} kept "
            f"{DIM}(last {PERFORMANCE_WINDOW_MINUTES} min){RESET}",
            "OCR is working and the overlay filter is discarding its answers. "
            "More shards will not help. Restart the anpr workers to clear the "
            "filter, and check services/anpr/overlay.py still forgets text it "
            "has not seen for ANPR_OVERLAY_MAX_GAP_S.",
        )
    fraction = shed.get("ocr_shed_fraction")
    if fraction is None:
        return Shot("OCR keeping up", True, f"{DIM}not reported{RESET}")
    ready = fraction <= MAX_SHED_FRACTION
    detail = (f"{fraction:.1%} of OCR attempts shed ({shed.get('ocr_shed', 0)} dropped) "
              f"{DIM}(last {PERFORMANCE_WINDOW_MINUTES} min){RESET}")
    advice = ""
    if not ready:
        advice = (
            "the ANPR tier cannot keep up, and this is why the trace is empty "
            "and the report has no plates. Add shards: set ANPR_SHARDS=N in "
            ".env *and* `docker compose up -d --scale anpr=N` — scaling alone "
            "starts replicas that claim nothing and sit at 0% CPU. Do not raise "
            "ANPR_OCR_CONCURRENCY; on a saturated box it measured worse."
        )
    return Shot("OCR keeping up", ready, detail, advice)


def check_report(api: str, token: str | None) -> Shot:
    """Deliverable 4 leads with this, so it must not be discovered empty."""
    try:
        payload = _get(api, "/api/sightings?limit=25", token)
    except Exception as exc:  # noqa: BLE001
        return Shot("Report export has rows", False, f"{RED}{exc}{RESET}", "")
    rows = payload.get("sightings", payload) if isinstance(payload, dict) else payload
    with_plate = [r for r in rows if (r.get("plate_normalised") or "").strip()]
    ready = len(with_plate) >= 5
    return Shot("Report export has rows", ready,
                f"{len(with_plate)} of {len(rows)} recent sightings carry a plate",
                "" if ready else "no readable plates recently — is ANPR keeping up?")


def run_once(api: str, token: str | None) -> list[Shot]:
    estate = check_estate(api, token)
    if not estate.ready:
        return [estate]
    trace, _ = check_trace(api, token)
    return [
        estate,
        # Before the shots, because when this is red it explains most of them.
        check_ocr_keeping_up(api, token),
        trace,
        check_trace_is_not_filmed_unbounded(api, token),
        check_alert(api, token),
        check_backend_proof(api, token),
        check_report(api, token),
    ]


def render(shots: list[Shot]) -> str:
    lines = [
        "",
        f"{BOLD}Demo readiness{RESET}",
        f"{DIM}nothing here writes, seeds or deletes; it only looks{RESET}",
        "",
    ]
    lines += [s.render() for s in shots]
    waiting = [s for s in shots if not s.ready]
    lines.append("")
    if waiting:
        lines.append(f"  {AMBER}{len(waiting)} of {len(shots)} not ready{RESET}")
    else:
        lines.append(f"  {GREEN}All shots ready — record now.{RESET}")
        lines.append(f"  {DIM}Set the trace panel's window to "
                     f"'Last {CLIP_CYCLE_MINUTES} min'-equivalent before rolling; "
                     f"'All time' is the default and is not the shot.{RESET}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Wait until each demo shot is filmable.")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--token", help="bearer token, if the instance has auth on")
    parser.add_argument("--watch", action="store_true", help="poll until everything is ready")
    parser.add_argument("--interval", type=float, default=20.0)
    parser.add_argument("--timeout-minutes", type=float, default=40.0,
                        help="give up after this long; one clip cycle is 25 min")
    args = parser.parse_args(argv)

    deadline = time.monotonic() + args.timeout_minutes * 60
    while True:
        shots = run_once(args.api, args.token)
        print(render(shots))
        if all(s.ready for s in shots):
            return 0
        if not args.watch:
            return 1
        if time.monotonic() > deadline:
            log.info("gave up after %.0f minutes", args.timeout_minutes)
            return 2
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
