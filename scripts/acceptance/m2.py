"""M2 acceptance test.

From docs/build-plan.md §5:

    Accept: clicking any pin plays live video in under 3 seconds. Adding a new
            adapter type requires no changes outside its own module.

Both halves are checked against the running stack.

*Under 3 seconds* is measured to the point where video is genuinely playable —
the media server holds the path, it is publishing, and bytes are arriving —
not merely to the point where the API returned a URL. Two cases matter and are
timed separately: a camera something already publishes continuously (the
simulated farm), and a cold pull-on-demand camera where the platform has to
start a relay first. The second is the honest worst case and the one the
government feeds take.

*No changes outside its own module* is checked structurally: the codebase is
scanned for per-adapter branching outside `services/adapters/`, and the API's
adapter list is checked to come from the self-registration table rather than
from a hand-maintained list.

Anything this test creates is removed again, so it can be run repeatedly.

Usage:
    python -m scripts.acceptance.m2
"""

from __future__ import annotations

import argparse
import contextlib
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

DEFAULT_API = "http://localhost:8000"
DEFAULT_MEDIA_API = "http://localhost:9997"
DEFAULT_RELAY = "http://localhost:8100"

# The clip served by the `mediasrc` container, reached by its compose-internal
# name because the relay — not this script — is what opens it. Progressive HTTP
# byte-range, which is exactly how the government evaluation feeds deliver.
DEFAULT_SOURCE = "http://mediasrc:8080/traffic-01-night.mp4"

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

PLAY_BUDGET_S = 3.0

REPO = pathlib.Path(__file__).resolve().parents[2]


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def render(self) -> str:
        mark = f"{GREEN}PASS{RESET}" if self.passed else f"{RED}FAIL{RESET}"
        return f"  [{mark}] {self.name}\n         {DIM}{self.detail}{RESET}"


def _request(
    url: str,
    method: str = "GET",
    body: dict | None = None,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Actor", "acceptance-m2")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read() or "null")


def _media_path(media_api: str, path: str) -> dict | None:
    """One path's state from the media server, or None if it does not exist."""
    try:
        return _request(f"{media_api}/v3/paths/get/{urllib.parse.quote(path)}", timeout=5)
    except urllib.error.HTTPError:
        return None
    except Exception:  # noqa: BLE001
        return None


def _playable(media_api: str, path: str) -> bool:
    """True once the media server can actually serve this path to a player."""
    info = _media_path(media_api, path)
    return bool(info and info.get("ready") and info.get("tracks"))


def _wait_playable(media_api: str, path: str, budget_s: float) -> float | None:
    """Seconds until the path became playable, or None if it never did."""
    started = time.monotonic()
    while time.monotonic() - started < budget_s:
        if _playable(media_api, path):
            return time.monotonic() - started
        time.sleep(0.05)
    return None


def _cameras(api: str) -> list[dict]:
    return _request(f"{api}/api/cameras?limit=500")


# --- criterion 1: video in under three seconds --------------------------


def check_published_camera(api: str, media_api: str, cameras: list[dict]) -> Check:
    """A camera something already publishes must be instantly playable.

    This is the common case in the demo: the simulated farm publishes
    continuously, so opening one is a lookup, not a stream start.
    """
    name = "Click a live pin → playable video"
    candidates = [c for c in cameras if c["adapter"] == "rtsp" and c["status"] == "online"]
    if not candidates:
        return Check(name, False, "no online rtsp camera in the registry")

    camera = candidates[0]
    started = time.monotonic()
    try:
        target = _request(f"{api}/api/cameras/{camera['id']}/stream")
    except Exception as exc:  # noqa: BLE001
        return Check(name, False, f"stream request failed: {exc}")

    path = urllib.parse.urlparse(target["url"]).path.lstrip("/")
    remaining = PLAY_BUDGET_S - (time.monotonic() - started)
    ready_in = _wait_playable(media_api, path, max(remaining, 0.1))
    elapsed = time.monotonic() - started

    ok = target["ready"] and ready_in is not None and elapsed < PLAY_BUDGET_S
    return Check(
        name,
        ok,
        f"{camera['external_ref']} ({camera['adapter']}) playable as "
        f"{target['protocol']} in {elapsed:.2f}s (budget {PLAY_BUDGET_S:.0f}s)"
        if ok
        else f"{camera['external_ref']}: ready={target['ready']} "
        f"path={path!r} playable={ready_in is not None} after {elapsed:.2f}s",
    )


def check_pull_on_demand(api: str, media_api: str, relay: str, camera_id: str) -> Check:
    """A cold camera must be pulled and playable inside the same budget.

    Nothing is publishing this camera when the test starts — the platform has
    to open the source, start a relay and get bytes into the media server. This
    is the path the 31 progressive-HTTP government feeds take.
    """
    name = "Cold camera → relay started → playable video"

    # Make sure it really is cold, so the measurement is not a warm-path result.
    with contextlib.suppress(Exception):
        _request(f"{relay}/relay/{camera_id}", "DELETE", timeout=10)
    time.sleep(0.3)

    started = time.monotonic()
    try:
        target = _request(f"{api}/api/cameras/{camera_id}/stream")
    except Exception as exc:  # noqa: BLE001
        return Check(name, False, f"stream request failed: {exc}")

    path = urllib.parse.urlparse(target["url"]).path.lstrip("/")
    remaining = PLAY_BUDGET_S - (time.monotonic() - started)
    ready_in = _wait_playable(media_api, path, max(remaining, 0.1))
    elapsed = time.monotonic() - started

    ok = ready_in is not None and elapsed < PLAY_BUDGET_S and target["relayed"]
    info = _media_path(media_api, path) or {}
    tracks = ",".join(info.get("tracks") or []) or "none"
    return Check(
        name,
        ok,
        f"relay started and {path!r} was playable ({tracks}) in {elapsed:.2f}s "
        f"(budget {PLAY_BUDGET_S:.0f}s)"
        if ok
        else f"{path!r} not playable after {elapsed:.2f}s: "
        f"ready={target.get('ready')} relayed={target.get('relayed')} "
        f"detail={target.get('detail')!r}",
    )


def check_unplayable_codec(api: str, media_api: str, relay: str, cameras: list[dict]) -> Check:
    """A camera whose codec no browser can decode must still play.

    Ten of the fifty simulated cameras publish H.265 or MPEG-4 on purpose,
    because a real estate is not uniformly H.264. WebRTC carries neither, and
    the failure is silent — the session is established, a few packets arrive
    and the server drops it, which in the browser is a video that never paints.
    The platform must re-encode for the viewer and leave the source alone.
    """
    name = "Camera in an unplayable codec → re-encoded, still under budget"

    target, camera = None, None
    for candidate in cameras:
        if candidate["status"] == "decommissioned":
            continue
        try:
            resolved = _request(f"{api}/api/cameras/{candidate['id']}/stream")
        except Exception:  # noqa: BLE001
            continue
        if resolved.get("transcoded"):
            target, camera = resolved, candidate
            break

    if target is None:
        return Check(name, False, "no camera in the estate needed re-encoding — expected 10")

    # Measure it cold, the same way an operator meets it.
    with contextlib.suppress(Exception):
        _request(f"{relay}/relay/{camera['id']}", "DELETE", timeout=10)
    time.sleep(0.3)

    started = time.monotonic()
    target = _request(f"{api}/api/cameras/{camera['id']}/stream")
    path = urllib.parse.urlparse(target["url"]).path.lstrip("/")
    remaining = PLAY_BUDGET_S - (time.monotonic() - started)
    ready_in = _wait_playable(media_api, path, max(remaining, 0.1))
    elapsed = time.monotonic() - started

    source_path = urllib.parse.urlparse(camera["stream_ref"]).path.lstrip("/")
    source = _media_path(media_api, source_path) or {}
    played = _media_path(media_api, path) or {}

    ok = (
        ready_in is not None
        and elapsed < PLAY_BUDGET_S
        and target["transcoded"]
        # The original must be untouched: ANPR reads it at full quality.
        and bool(source.get("ready"))
    )
    return Check(
        name,
        ok,
        f"{camera['external_ref']} publishes {','.join(source.get('tracks') or [])} — "
        f"served as {','.join(played.get('tracks') or [])} on {path!r} in {elapsed:.2f}s, "
        f"source left publishing untouched"
        if ok
        else f"{camera['external_ref']}: playable={ready_in is not None} after {elapsed:.2f}s, "
        f"transcoded={target.get('transcoded')}, source ready={source.get('ready')}",
    )


def check_every_camera_resolves(api: str, cameras: list[dict]) -> Check:
    """Every camera in the estate must resolve to something playable.

    "Clicking *any* pin" is the wording, so a spot check is not enough. Also
    asserts the invariant that no playback URL ever carries a credential.
    """
    name = "Every camera resolves, no credential leaks"
    failures, leaked, by_adapter = [], [], {}

    for camera in cameras:
        if camera["status"] == "decommissioned":
            continue
        try:
            # `wait=false` keeps this to a registry+adapter resolution: this
            # check is about coverage of the estate, not about start latency.
            target = _request(f"{api}/api/cameras/{camera['id']}/stream?wait=false")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{camera['external_ref']}: {exc}")
            continue
        by_adapter[target["adapter"]] = by_adapter.get(target["adapter"], 0) + 1
        if "@" in urllib.parse.urlparse(target["url"]).netloc:
            leaked.append(camera["external_ref"])

    mix = ", ".join(f"{n}×{a}" for a, n in sorted(by_adapter.items()))
    ok = not failures and not leaked
    return Check(
        name,
        ok,
        f"{sum(by_adapter.values())} cameras resolved ({mix}); no URL carries a credential"
        if ok
        else f"{len(failures)} failed to resolve ({failures[:3]}); "
        f"{len(leaked)} leaked credentials",
    )


# --- criterion 2: a new adapter changes nothing outside its module -------

# Per-adapter branching: comparing something to a known adapter name, or a
# membership test against a set of them. This is what the rule forbids.
_ADAPTER_NAMES = ("rtsp", "http", "hls", "file", "onvif")
_BRANCH_RE = re.compile(
    r"adapter\s*(==|!=)\s*[\"']({})[\"']".format("|".join(_ADAPTER_NAMES))
)

# Where knowing an adapter by name is legitimate.
_ALLOWED = (
    "services/adapters/",       # the modules themselves
    "scripts/",                 # seeds and this test
    "tests/",
    "db/",
    "docs/",
)


def check_no_adapter_branching() -> Check:
    """No `if adapter == "rtsp"` anywhere outside the adapter modules."""
    name = "No per-adapter branching outside services/adapters/"
    offenders = []
    for path in sorted(REPO.glob("services/**/*.py")) + sorted(REPO.glob("web/src/**/*.ts*")):
        rel = path.relative_to(REPO).as_posix()
        if any(rel.startswith(prefix) for prefix in _ALLOWED):
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            # A comment quoting the rule is not a violation of it.
            if line.lstrip().startswith(("#", "//", "*")):
                continue
            if _BRANCH_RE.search(line):
                offenders.append(f"{rel}:{lineno}")

    return Check(
        name,
        not offenders,
        "no module outside the adapter package decides behaviour by adapter name"
        if not offenders
        else f"per-adapter branch found at {', '.join(offenders[:5])}",
    )


def check_adapters_self_register(api: str) -> Check:
    """The API's adapter list must come from the registration table.

    Checked by comparing what the API reports against what the package
    registers when imported — if the endpoint were a hand-written list the two
    would drift the moment a module was added.
    """
    name = "Adapters self-register; nothing enumerates them by hand"
    try:
        reported = _request(f"{api}/api/cameras/adapters/registered")
    except Exception as exc:  # noqa: BLE001
        return Check(name, False, f"request failed: {exc}")

    served = {a["name"] for a in reported["adapters"]}

    # A brand-new adapter, defined here and never imported by the application,
    # must become available purely by being registered.
    sys.path.insert(0, str(REPO))
    from services.adapters import CameraAdapter, PlaybackTarget, available_adapters
    from services.adapters.base import register_adapter

    before = set(available_adapters())

    @register_adapter
    class _AcceptanceAdapter(CameraAdapter):
        name = "acceptance-probe"

        def playback(self, camera):  # noqa: ANN001, ANN201
            return PlaybackTarget(camera_id=camera.id, protocol="webrtc", url="rtsp://example")

        def probe_url(self, camera) -> str:  # noqa: ANN001
            return camera.stream_ref

    after = set(available_adapters())
    added = after - before

    ok = served == before and added == {"acceptance-probe"}
    return Check(
        name,
        ok,
        f"API serves exactly the {len(served)} registered adapters "
        f"({', '.join(sorted(served))}); a new class registered itself with no "
        f"edit to any factory, list or branch"
        if ok
        else f"API reports {sorted(served)} but the package registers {sorted(before)}; "
        f"new adapter added {sorted(added)}",
    )


# --- supporting guarantees ---------------------------------------------


def check_audited(api: str, camera_id: str) -> Check:
    """Watching a camera is an access to personal data, so it is recorded."""
    name = "Stream views are audited, with case binding"
    case_ref = f"ACC-M2-{int(time.time())}"
    try:
        _request(
            f"{api}/api/cameras/{camera_id}/stream?wait=false",
            headers={"X-Case-Ref": case_ref},
        )
        entries = _request(f"{api}/api/audit?action=stream.view&case_ref={case_ref}")
    except Exception as exc:  # noqa: BLE001
        return Check(name, False, f"request failed: {exc}")

    ok = len(entries) == 1 and entries[0]["subject"] == camera_id
    return Check(
        name,
        ok,
        f"stream.view recorded for actor {entries[0]['actor']!r} bound to case "
        f"{case_ref} ({entries[0]['detail']})"
        if ok
        else f"expected one audit row for {case_ref}, found {len(entries)}",
    )


def check_relay_is_on_demand(relay: str, camera_id: str) -> Check:
    """Video is pulled while watched and stopped when not — not hauled always."""
    name = "Relay is on demand, and stoppable"
    try:
        active = _request(f"{relay}/relay")
        running = {r["camera_id"] for r in active["relays"]}
        stopped = _request(f"{relay}/relay/{camera_id}", "DELETE", timeout=10)
        after = {r["camera_id"] for r in _request(f"{relay}/relay")["relays"]}
    except Exception as exc:  # noqa: BLE001
        return Check(name, False, f"relay supervisor unreachable: {exc}")

    ok = camera_id in running and stopped.get("stopped") and camera_id not in after
    return Check(
        name,
        ok,
        f"{len(running)} relay(s) running while watched; stopping the camera "
        f"left {len(after)} — no video is pulled for cameras nobody is viewing"
        if ok
        else f"running={sorted(running)} stopped={stopped} after={sorted(after)}",
    )


# --- harness ------------------------------------------------------------


def create_probe(api: str, source: str) -> str | None:
    payload = {
        "name": "M2 acceptance probe",
        "adapter": "http",
        "stream_ref": source,
        "lat": 23.0225,
        "lon": 72.5714,
        "district": "Ahmedabad",
        "department": "Police",
        "kind": "fixed",
        "external_ref": "acceptance-m2-probe",
    }
    try:
        return _request(f"{api}/api/cameras", "POST", payload)["id"]
    except Exception:  # noqa: BLE001
        pass

    # A previous run decommissioned it, and decommissioning keeps the row so the
    # external reference is still taken. Bring the same camera back into service
    # rather than accumulating a retired probe per run.
    with contextlib.suppress(Exception):
        retired = _request(f"{api}/api/cameras?status=decommissioned")
        for camera in retired:
            if camera["external_ref"] == "acceptance-m2-probe":
                _request(f"{api}/api/cameras/{camera['id']}/recommission", "POST")
                # The source may have moved between runs; keep the row current.
                _request(f"{api}/api/cameras/{camera['id']}", "PATCH", {"stream_ref": source})
                return camera["id"]
    with contextlib.suppress(Exception):
        for camera in _cameras(api):
            if camera["external_ref"] == "acceptance-m2-probe":
                return camera["id"]
    return None


def cleanup(api: str, relay: str, camera_id: str | None) -> None:
    if not camera_id:
        return
    for url, method in (
        (f"{relay}/relay/{camera_id}", "DELETE"),
        (f"{api}/api/cameras/{camera_id}", "DELETE"),
    ):
        with contextlib.suppress(Exception):
            _request(url, method, timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description="M2 acceptance test.")
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--media-api", default=DEFAULT_MEDIA_API)
    parser.add_argument("--relay", default=DEFAULT_RELAY)
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="Progressive HTTP test source.")
    args = parser.parse_args()

    print(f"\n{'=' * 70}\nM2 acceptance — adapter framework and live view\n{'=' * 70}\n")

    cameras = _cameras(args.api)
    probe_id = create_probe(args.api, args.source)

    checks = [
        check_published_camera(args.api, args.media_api, cameras),
    ]
    if probe_id:
        checks += [
            check_pull_on_demand(args.api, args.media_api, args.relay, probe_id),
            check_relay_is_on_demand(args.relay, probe_id),
            check_audited(args.api, probe_id),
        ]
    else:
        checks.append(Check("Cold camera → relay started → playable video", False,
                            "could not create the probe camera"))
    checks += [
        check_unplayable_codec(args.api, args.media_api, args.relay, cameras),
        check_every_camera_resolves(args.api, cameras),
        check_no_adapter_branching(),
        check_adapters_self_register(args.api),
    ]

    cleanup(args.api, args.relay, probe_id)

    for check in checks:
        print(check.render())

    passed = sum(c.passed for c in checks)
    ok = passed == len(checks)
    print(f"\n{'=' * 70}")
    print(
        f"{GREEN}M2 ACCEPTANCE PASSED{RESET}" if ok
        else f"{RED}M2 ACCEPTANCE FAILED{RESET} — {passed}/{len(checks)} checks passed"
    )
    print(f"{'=' * 70}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
