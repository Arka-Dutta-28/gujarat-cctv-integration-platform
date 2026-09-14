"""M3 acceptance test.

From docs/build-plan.md §5:

    Accept: 50 streams processing concurrently; `sightings` filling; measured
            throughput and per-stage latency logged.

Everything here is read from what the running pipeline recorded. Nothing is
configured, assumed, or taken from a constant in this file — the point of the
test is that the numbers in the submission came from the system.

Two checks are not in the build plan's wording and are the most important ones
in the file.

`check_every_read_persisted` verifies that reads which *fail* the Indian plate
format are present in `sightings`. Invariant 1 is that every
read is persisted, and the easiest way to accidentally break it is a
well-meaning quality filter somewhere in the write path. A test that only
looked at valid plates would pass happily against a pipeline that had silently
started discarding the noisy ones — and the noisy one is sometimes the wanted
vehicle.

`check_plates_look_like_plates` verifies that the plate column holds plates.
That sounds redundant until it isn't: this file once passed all seven of its
other checks against a table in which every plate was a Python dataclass repr.
Counting rows tells you the pipeline is running, not that it is working.

Usage:
    python -m scripts.acceptance.m3
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from dataclasses import dataclass

DEFAULT_API = "http://localhost:8000"
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

#: The build plan's figure. The simulated farm is 50 cameras.
REQUIRED_CAMERAS = 50

#: How long to watch before judging, when the pipeline has just started.
DEFAULT_OBSERVE_S = 90.0


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
    req.add_header("X-Actor", "acceptance-m3")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read() or "null")


def check_concurrency(api: str, required: int) -> Check:
    """Criterion 1: streams processing concurrently."""
    name = f"{required} streams processing concurrently"
    perf = _get(f"{api}/api/performance?minutes=15")
    cameras = perf.get("cameras_processing", 0)
    fps = perf.get("mean_decode_fps")
    return Check(
        name,
        cameras >= required,
        f"{cameras} cameras decoding, mean {fps} fps each; "
        f"{perf.get('frames_decoded', 0):,} frames decoded in the window"
        if cameras >= required
        else f"only {cameras} cameras reported frames in the last 15 minutes",
    )


def check_sightings_filling(api: str, observe_s: float) -> Check:
    """Criterion 2: `sightings` filling — measured as a rate, not a total."""
    name = "Sightings accumulating from live streams"
    before = _get(f"{api}/api/sightings/stats")
    started = time.monotonic()
    time.sleep(min(observe_s, 30.0))
    after = _get(f"{api}/api/sightings/stats")
    elapsed = time.monotonic() - started

    gained = after["total"] - before["total"]
    return Check(
        name,
        gained > 0,
        f"+{gained} sightings in {elapsed:.0f}s ({gained / elapsed * 60:.1f}/min); "
        f"{after['total']:,} total from {after['cameras_contributing']} cameras, "
        f"{after['distinct_plates']:,} distinct plates"
        if gained > 0
        else f"no new sightings in {elapsed:.0f}s (total stuck at {after['total']})",
    )


def check_instrumentation(api: str) -> Check:
    """Criterion 3: per-stage latency measured and logged."""
    name = "Per-stage latency and throughput measured"
    perf = _get(f"{api}/api/performance?minutes=15")
    stages = {s["stage"]: s for s in perf.get("stages", [])}
    expected = {"vehicle_detect", "plate_detect", "ocr"}
    have = expected & set(stages)

    ok = have == expected and all(stages[s]["samples"] > 0 for s in have)
    summary = ", ".join(
        f"{s}: p50 {stages[s]['p50_ms']}ms / p95 {stages[s]['p95_ms']}ms" for s in sorted(have)
    )
    return Check(
        name,
        ok,
        summary if ok else f"missing timings for {sorted(expected - have)} (have {sorted(stages)})",
    )


def check_every_read_persisted(api: str) -> Check:
    """The project's first invariant, checked against the live index."""
    name = "Every read persisted, including format failures"
    stats = _get(f"{api}/api/sightings/stats")
    total = stats["total"]
    valid = stats["format_valid"]
    invalid = total - valid

    return Check(
        name,
        total > 0 and invalid > 0,
        f"{total:,} sightings: {valid:,} format-valid, {invalid:,} kept and flagged "
        f"— no quality filter in the write path"
        if total > 0 and invalid > 0
        else (
            "no sightings at all" if total == 0
            else f"all {total:,} sightings are format-valid, which is implausible: "
            "something is filtering reads before they are written"
        ),
    )


#: Longest plausible normalised Indian plate is 11 characters
#: (2 state + 2 RTO + 3 series + 4 number). Anything far beyond that is not a
#: misread — it is a different kind of value sitting in the plate column.
MAX_PLAUSIBLE_PLATE_LEN = 16

#: A floor, not a target. The lean path measured 89.5% format-valid and a
#: learned model on unfamiliar plates will do worse, so this sits where it only
#: trips when something is structurally wrong rather than merely inaccurate.
MIN_FORMAT_VALID_RATE = 0.20


def check_plates_look_like_plates(api: str) -> Check:
    """Do the stored plates resemble plates at all?

    This check exists because every other check in this file once passed, all
    seven of them, against a `sightings` table in which every plate was a Python
    dataclass repr — `PlatePrediction(plate='2J2ZN4469', char_probs=array(...))`
    — normalised into a 150-character key. 50 streams were processing, sightings
    were filling, per-stage latencies were recorded, format failures were kept.
    Every assertion was true and the ANPR output was worthless.

    The gap was that nothing ever looked at a value. Counting rows tells you the
    pipeline is running; it cannot tell you the pipeline is working.
    """
    name = "Stored plates are plate-shaped"
    sightings = _get(f"{api}/api/sightings?limit=500")
    if not sightings:
        return Check(name, False, "no sightings to inspect")

    keys = [(s.get("plate_normalised") or "") for s in sightings]
    overlong = [k for k in keys if len(k) > MAX_PLAUSIBLE_PLATE_LEN]
    longest = max(keys, key=len)

    stats = _get(f"{api}/api/sightings/stats")
    rate = stats["format_valid"] / stats["total"] if stats["total"] else 0.0

    ok = not overlong and rate >= MIN_FORMAT_VALID_RATE
    if ok:
        detail = (
            f"{len(keys)} sampled, longest {len(longest)} chars ({longest}); "
            f"{rate:.1%} of all sightings are format-valid"
        )
    elif overlong:
        detail = (
            f"{len(overlong)}/{len(keys)} plates exceed {MAX_PLAUSIBLE_PLATE_LEN} "
            f"characters — the plate column is holding something that is not a "
            f"plate. Longest: {longest[:80]}"
        )
    else:
        detail = (
            f"only {rate:.1%} of sightings are format-valid (floor "
            f"{MIN_FORMAT_VALID_RATE:.0%}) — the reader is not reading plates"
        )
    return Check(name, ok, detail)


def check_condition_reporting(api: str) -> Check:
    """Accuracy must be reportable per condition, not as one headline number."""
    name = "Accuracy reportable per condition"
    stats = _get(f"{api}/api/sightings/stats")
    conditions = {c["condition"]: c for c in stats.get("by_condition", [])}
    real = {c for c in conditions if c != "unknown"}

    return Check(
        name,
        len(real) >= 2,
        "; ".join(
            f"{c}: {conditions[c]['sightings']:,} sightings, "
            f"{conditions[c]['format_valid']:,} valid, "
            f"mean confidence {conditions[c]['mean_confidence']}"
            for c in sorted(real)
        )
        if len(real) >= 2
        else f"only {sorted(conditions)} seen — cannot split accuracy by condition yet",
    )


def check_sampling_saving(api: str) -> Check:
    """The number the 80,000-camera argument rests on."""
    name = "Adaptive sampling saving is measured"
    perf = _get(f"{api}/api/performance?minutes=15")
    fraction = perf.get("analysed_fraction")
    return Check(
        name,
        fraction is not None and 0 < fraction < 1,
        f"{perf['frames_analysed']:,} of {perf['frames_decoded']:,} decoded frames "
        f"reached a detector ({fraction:.1%}) — {perf.get('detector_load_avoided')}"
        if fraction
        else "no decode/analyse counters recorded",
    )


def check_per_track_voting(api: str) -> Check:
    """One sighting per vehicle, voted from many frames — not one per frame."""
    name = "One sighting per tracked vehicle, voted from many reads"
    sightings = _get(f"{api}/api/sightings?limit=500")
    if not sightings:
        return Check(name, False, "no sightings to inspect")

    multi = [s for s in sightings if (s.get("read_count") or 0) > 1]
    mean_reads = sum(s.get("read_count") or 0 for s in sightings) / len(sightings)
    # Per-frame emission shows up as many rows for one track id on one camera.
    keys = [(s["camera_id"], s.get("track_id")) for s in sightings if s.get("track_id")]
    duplicates = len(keys) - len(set(keys))

    ok = bool(multi) and duplicates == 0
    return Check(
        name,
        ok,
        f"{len(multi)}/{len(sightings)} sightings voted from more than one read "
        f"(mean {mean_reads:.1f} reads per sighting); no track emitted twice"
        if ok
        else f"{duplicates} duplicate (camera, track) rows; "
        f"{len(multi)}/{len(sightings)} multi-read",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="M3 acceptance test.")
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--cameras", type=int, default=REQUIRED_CAMERAS)
    parser.add_argument("--observe", type=float, default=DEFAULT_OBSERVE_S)
    args = parser.parse_args()

    print(f"\n{'=' * 70}\nM3 acceptance — ANPR pipeline\n{'=' * 70}\n")

    checks = [
        check_concurrency(args.api, args.cameras),
        check_sightings_filling(args.api, args.observe),
        check_instrumentation(args.api),
        check_every_read_persisted(args.api),
        check_plates_look_like_plates(args.api),
        check_condition_reporting(args.api),
        check_sampling_saving(args.api),
        check_per_track_voting(args.api),
    ]

    for check in checks:
        print(check.render())

    passed = sum(c.passed for c in checks)
    ok = passed == len(checks)
    print(f"\n{'=' * 70}")
    print(
        f"{GREEN}M3 ACCEPTANCE PASSED{RESET}" if ok
        else f"{RED}M3 ACCEPTANCE FAILED{RESET} — {passed}/{len(checks)} checks passed"
    )
    print(f"{'=' * 70}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
