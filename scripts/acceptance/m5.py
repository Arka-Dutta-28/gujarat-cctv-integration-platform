"""M5 acceptance test.

From docs/build-plan.md section 5:

    Accept: adding a plate to the watchlist causes the next sighting of it to
            raise an alert in the UI within 5 seconds, with crop, camera, time
            and map pin attached.

The test is deliberately live and end to end. It adds a plate to the watchlist
through the public API and then waits for the running pipeline to see that
vehicle and raise an alert. Nothing is injected into sightings and no part of
the alerting path is stubbed, because the point of this milestone is that the
loop closes through a camera.

Two lessons from earlier milestones shape it. M3's acceptance passed 7/7
against a table where every plate was a Python dataclass repr, because every
check counted rows and none read a value; so these checks read the alert's
contents: the tier, the crop, the coordinates. M4 opened by discovering the
harness could not produce a journey at all, so the plate used here is one that
the ground truth in data/test-videos/manifest.json says genuinely passes a
camera, rather than one this file invents.

Usage:
    python -m scripts.acceptance.m5
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

DEFAULT_API = "http://localhost:8000"
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

#: The planted **alerting** plate — in the watchlist, unlike M4's trace plate.
#: Chosen from the generated clips' manifest so the whole chain is provable:
#: ground truth says the vehicle passed, `sightings` says the platform read it,
#: `alerts` says the watchlist caught it. Seeded by `scripts/seed.py`.
PLANTED_PLATE = "GJ05UV9972"

#: build-plan §5. Detection to alert.
BUDGET_S = 5.0

#: How long to wait for the vehicle to come round again. This is *not* part of
#: the budget: the clips loop, so an alert cannot arrive until the car does. The
#: graded latency is measured from the sighting, not from the moment we asked.
WAIT_FOR_PASS_S = 420.0

#: Gujarat's bounding box, as in M4 — catches a transposed [lon, lat] pin.
GUJARAT_BBOX = (68.0, 20.0, 74.7, 24.8)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def render(self) -> str:
        mark = f"{GREEN}PASS{RESET}" if self.passed else f"{RED}FAIL{RESET}"
        return f"  [{mark}] {self.name}\n         {DIM}{self.detail}{RESET}"


def _request(url: str, method: str = "GET", body: dict | None = None, timeout: float = 30.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Actor", "acceptance-m5")
    req.add_header("X-Case-Ref", "ACC-M5")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read() or "null")


def _alerts_for(api: str, plate: str, since: datetime | None = None) -> list[dict]:
    query = f"{api}/api/alerts?limit=200"
    if since:
        query += "&since=" + urllib.parse.quote(since.isoformat())
    return [a for a in _request(query) if a["plate"].replace(" ", "").upper() == plate]


# --- checks --------------------------------------------------------------


def check_watchlist_accepts_a_plate(api: str, plate: str) -> tuple[Check, dict | None]:
    """Adding through the API is what an operator actually does."""
    name = "A plate can be added to the watchlist"
    try:
        entry = _request(
            f"{api}/api/watchlist", "POST",
            {"plate": plate, "category": "stolen", "severity": 5,
             "source": "acceptance", "case_ref": "ACC-M5"},
        )
    except urllib.error.HTTPError as exc:
        # Already seeded is the normal case on a re-run, not a failure.
        existing = [
            e for e in _request(f"{api}/api/watchlist")
            if e["plate_normalised"] == plate
        ]
        if existing:
            return Check(
                name, True,
                f"already on the watchlist as {existing[0]['category']} "
                f"(severity {existing[0]['severity']}), {existing[0]['alerts']} alerts "
                "raised to date",
            ), existing[0]
        return Check(name, False, f"POST /api/watchlist returned {exc.code}"), None

    return Check(
        name, entry["active"] and entry["plate_normalised"] == plate,
        f"{entry['plate']} normalised to {entry['plate_normalised']}, "
        f"{entry['category']}, severity {entry['severity']}",
    ), entry


def check_an_alert_is_raised(
    api: str, plate: str, wait_s: float, since: datetime
) -> tuple[Check, dict | None]:
    """The loop closes: a watched vehicle passes a camera and an alert appears.

    Scoped to alerts raised after this run started, which is the difference between
    observing the loop close and finding evidence that it once did. An earlier
    version accepted any existing alert and passed instantly against a pipeline
    that could have been stopped for an hour.

    The wait is generous because the clips loop: the vehicle has to come round again
    before there is anything to detect. That waiting is not part of the graded
    budget, which is measured from the sighting.
    """
    name = "The next sighting of the vehicle raises a new alert"
    started = time.monotonic()
    deadline = started + wait_s
    while time.monotonic() < deadline:
        alerts = _alerts_for(api, plate, since)
        if alerts:
            waited = time.monotonic() - started
            newest = alerts[0]
            return Check(
                name, True,
                f"alert on {newest['plate']} at {newest['camera_name']} "
                f"({newest['district']}), tier {newest['tier']}, raised "
                f"{waited:.0f}s into this run — the vehicle had to come round "
                "again first, which is why the wait is not the graded number",
            ), newest
        time.sleep(5)

    return Check(
        name, False,
        f"no new alert for {plate} within {wait_s:.0f}s. Is the ANPR tier running, "
        f"is /api/performance write_health healthy, and does the estate still carry "
        f"the clip this plate rides?",
    ), None


def check_detection_latency(alert: dict | None, budget: float) -> Check:
    """The graded number: sighting written to alert raised."""
    name = f"Detection to alert under {budget:.0f}s"
    if alert is None:
        return Check(name, False, "no alert to measure")
    latency = alert["detection_latency_s"]
    return Check(
        name, latency < budget,
        f"{latency * 1000:.0f} ms from the sighting being written to the alert "
        f"existing (budget {budget:.0f}s). Matching runs in the same transaction "
        "as the write, so the queue hop a separate consumer would add is not there",
    )


def check_evidence_card_is_complete(api: str, alert: dict | None) -> Check:
    """build-plan §5 names four things by name: crop, camera, time, map pin."""
    name = "Alert carries crop, camera, time and map pin"
    if alert is None:
        return Check(name, False, "no alert to inspect")

    missing = []
    if not alert.get("camera_name"):
        missing.append("camera")
    if not alert.get("sighting_ts"):
        missing.append("time")
    lat, lon = alert.get("lat"), alert.get("lon")
    if lat is None or lon is None:
        missing.append("map pin")
    elif not (GUJARAT_BBOX[0] <= lon <= GUJARAT_BBOX[2]
              and GUJARAT_BBOX[1] <= lat <= GUJARAT_BBOX[3]):
        missing.append(f"map pin inside Gujarat (got {lat:.3f}, {lon:.3f})")

    crop = alert.get("thumbnail_url")
    if not crop:
        missing.append("crop")
    else:
        try:
            req = urllib.request.Request(f"{api}{crop}")
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                image = resp.read()
            if len(image) < 500 or image[:2] != b"\xff\xd8":
                missing.append(f"crop is not a JPEG ({len(image)} bytes)")
        except Exception as exc:  # noqa: BLE001
            missing.append(f"crop not retrievable ({exc})")

    return Check(
        name, not missing,
        f"{alert['camera_name']} at {alert['sighting_ts']}, pin ({lat}, {lon}), "
        f"crop served from {crop}"
        if not missing else f"missing: {', '.join(missing)}",
    )


def check_tiers_are_used(api: str, alert: dict | None) -> Check:
    """A match is not a boolean. The tier must be a real judgement."""
    name = "The match is tiered, not a boolean"
    if alert is None:
        return Check(name, False, "no alert to inspect")

    tier, read, wanted = alert["tier"], alert["plate_read"], alert["plate"]
    if tier not in ("confirmed", "probable", "possible"):
        return Check(name, False, f"unknown tier {tier!r}")

    exact = read == wanted
    consistent = (
        (tier == "confirmed" and exact and alert["confidence"] >= 0.85)
        or (tier == "probable" and (exact or _distance(read, wanted) <= 1))
        or (tier == "possible" and _distance(read, wanted) <= 2)
    )
    return Check(
        name, consistent,
        f"read {read!r} against watchlist {wanted!r} at confidence "
        f"{alert['confidence']:.2f} -> {tier}, priority {alert['priority']} "
        f"(severity {alert['severity']} demoted by certainty)"
        if consistent
        else f"tier {tier} does not follow from read {read!r} vs {wanted!r} at "
             f"confidence {alert['confidence']:.2f}",
    )


def _distance(a: str, b: str) -> int:
    from services.common.plates import edit_distance

    return edit_distance(a, b, cap=3)


def check_alerting_did_not_replace_the_index(api: str, plate: str) -> Check:
    """Invariant 1, checked from the alerting side.

    The temptation this milestone creates is to store only what matched. The
    index must still hold vastly more sightings than there are alerts, and it
    must hold reads of vehicles nobody is watching — that is what makes a trace
    possible for a plate handed over after the fact.
    """
    name = "Every read is still indexed, not only the matches"
    stats = _request(f"{api}/api/sightings/stats")
    alerts = _request(f"{api}/api/alerts/stats?hours=24")
    watched = {e["plate_normalised"] for e in _request(f"{api}/api/watchlist")}
    unwatched = [
        s for s in _request(f"{api}/api/sightings?limit=200")
        if s["plate_normalised"] not in watched
    ]
    ok = stats["total"] > alerts["alerts"] and len(unwatched) > 0
    return Check(
        name, ok,
        f"{stats['total']} sightings indexed against {alerts['alerts']} alerts in 24h; "
        f"{len(unwatched)} of the last 200 reads are vehicles nobody is watching",
    )


def check_alert_can_be_acknowledged(api: str, alert: dict | None) -> Check:
    """An alert nobody can action is a notification, not a console."""
    name = "An alert can be acknowledged and the outcome persists"
    if alert is None:
        return Check(name, False, "no alert to acknowledge")
    updated = _request(
        f"{api}/api/alerts/{alert['id']}/status", "POST",
        {"status": "acknowledged", "note": "M5 acceptance"},
    )
    persisted = _request(f"{api}/api/alerts?limit=200")
    row = next((a for a in persisted if a["id"] == alert["id"]), None)
    ok = (
        updated["status"] == "acknowledged"
        and row is not None
        and row["status"] == "acknowledged"
        and row["acknowledged_by"] == "acceptance-m5"
    )
    return Check(
        name, ok,
        f"acknowledged by {updated['acknowledged_by']}, note recorded, and the "
        "alert is still listed — dismissal is a status, never a delete"
        if ok else f"status did not persist: {updated.get('status')}",
    )


def check_alerting_is_separate_from_trace(api: str, plate: str) -> Check:
    """Invariant 2, made checkable.

    Both paths must independently answer for the same vehicle: alerting must
    have raised something, and the historical trace must return the same
    vehicle's sightings without any alert being involved.
    """
    name = "Trace and alerting are separate paths over the same index"
    journey = _request(f"{api}/api/vehicles/{urllib.parse.quote(plate)}/journey")
    visits = journey.get("properties", {}).get("visits", 0)
    alerts = len(_alerts_for(api, plate))
    return Check(
        name, visits > 0 and alerts > 0,
        f"the trace endpoint reconstructs {visits} visits for {plate} from the "
        f"index alone, while alerting independently raised {alerts} alerts for it",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="M5 acceptance test.")
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--plate", default=PLANTED_PLATE)
    parser.add_argument("--budget", type=float, default=BUDGET_S)
    parser.add_argument("--wait", type=float, default=WAIT_FOR_PASS_S)
    args = parser.parse_args()

    print(f"\n{'=' * 70}\nM5 acceptance — watchlist and live alerting\n{'=' * 70}\n")
    print(f"{DIM}Planted alerting plate: {args.plate} "
          f"(the M4 trace plate is deliberately a different vehicle){RESET}\n")

    # The server's clock, not this process's: alerts are timestamped by the
    # database, and a few seconds of skew would silently widen or void the
    # "raised after we started" scope this test depends on.
    started_at = datetime.fromisoformat(_request(f"{args.api}/api/status")["now"])

    watchlist_check, _entry = check_watchlist_accepts_a_plate(args.api, args.plate)
    alert_check, alert = check_an_alert_is_raised(
        args.api, args.plate, args.wait, started_at
    )

    checks = [
        watchlist_check,
        alert_check,
        check_detection_latency(alert, args.budget),
        check_evidence_card_is_complete(args.api, alert),
        check_tiers_are_used(args.api, alert),
        check_alerting_did_not_replace_the_index(args.api, args.plate),
        check_alert_can_be_acknowledged(args.api, alert),
        check_alerting_is_separate_from_trace(args.api, args.plate),
    ]

    for check in checks:
        print(check.render())

    passed = sum(c.passed for c in checks)
    ok = passed == len(checks)
    print(f"\n{'=' * 70}")
    print(
        f"{GREEN}M5 ACCEPTANCE PASSED{RESET}" if ok
        else f"{RED}M5 ACCEPTANCE FAILED{RESET} — {passed}/{len(checks)} checks passed"
    )
    print(f"{'=' * 70}\n")
    print(f"{DIM}Run at {datetime.now(UTC).isoformat()}{RESET}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
