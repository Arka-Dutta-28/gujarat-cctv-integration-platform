"""One command that answers every question the live grid can only answer live.

The Sentinel grid is an external dependency that has already moved host once and
was, as of 31 Aug 2026, returning 502. When it comes back the window may be
short, so the checks that have to run against it are collected here rather than
spread across a session's worth of ad-hoc commands.

It writes nothing: no database, no registry, no state. That makes it safe to run
against a grid whose shape is still unknown, which is the point.

Four questions, in decreasing order of how much they change the plan.

1. What is the plate crop width? The old estate measured 66 px against the
   simulated estate's 276 px, and no government camera reached ANPR grade.
   Those feeds were web-transcoded progressive HTTP; this grid offers direct
   RTSP at source. If the downscale was the transcode's doing, the problem is
   gone. This single number decides whether the government-feed deliverable
   shows live ANPR or leans on the exported report.

2. Does the declared frame rate match the measured one? The reference says not
   to trust CAP_PROP_FPS. A disagreement here is the evidence that the PTS work
   was necessary.

3. What shape is the catalogue? Field coverage per logical field, so a missing
   alias shows up as a column of dashes rather than as a null in the registry
   three steps later.

4. Which transports actually open? RTSP is designated for inference and HLS is
   the documented fallback for a network that blocks 8554. Knowing which works
   from this network is an operational fact, not a guess.

Usage:
    make grid-check                                # the live grid
    python -m scripts.grid_check --file cat.json   # a saved response, offline
    python -m scripts.grid_check --cameras 3 --seconds 20
    python -m scripts.grid_check --no-video        # catalogue shape only
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import statistics
import sys
import time
import urllib.error

from services.adapters.catalogue import (
    CATALOGUE_PATH,
    CatalogueAuthRequired,
    CatalogueCamera,
    fetch_catalogue,
    opener_for,
    parse_catalogue,
)
from services.common.gazetteer import resolve

log = logging.getLogger("grid-check")

BASE = os.environ.get("SENTINEL_BASE", "https://live.sentinelgujarat.in")

BOLD, DIM, RED, GREEN, YELLOW, RESET = (
    "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[0m"
)

#: The plate width the OCR needs to have a chance. Measured, not chosen: on this
#: estate 94% of simulated crops cleared it and read 9.4 characters, while 15% of
#: government crops did and read 1.7. See README.md, Measured results.
USABLE_PLATE_PX = 80

#: Logical fields worth reporting coverage for. Not every catalogue carries all
#: of them and that is fine — what matters is knowing which, before onboarding.
REPORTED_FIELDS = (
    "source_id", "name", "location", "district", "codec", "container",
    "width", "height", "declared_fps", "bitrate_kbps", "live", "kind",
    "lat", "lon", "department",
)


def _ok(flag: bool) -> str:
    return f"{GREEN}yes{RESET}" if flag else f"{RED}no{RESET}"


# --- the catalogue ------------------------------------------------------


def load(args: argparse.Namespace) -> tuple[list[CatalogueCamera], object]:
    """Cameras plus the raw payload, so the payload can be saved verbatim."""
    if args.file:
        with open(args.file, encoding="utf-8") as handle:
            payload = json.load(handle)
        return parse_catalogue(payload, base_url=BASE), payload

    # Fetched twice on purpose: `fetch_catalogue` carries the redirect handling
    # and the shape tolerance, and we also want the bytes exactly as served so a
    # later session can work offline against the same response.
    cameras = fetch_catalogue(BASE)
    # The raw copy goes through the signed-in opener too. A bare `urlopen` has no
    # session and Python's default user agent, and since 14 Sep the grid answers
    # that with 403 — so the check signed in, read 30 cameras, then called the
    # catalogue unreachable.
    url = BASE.rstrip("/") + CATALOGUE_PATH
    with opener_for(BASE).open(url, timeout=20) as response:
        payload = json.load(response)
    return cameras, payload


def report_coverage(cameras: list[CatalogueCamera]) -> None:
    """Which logical fields the catalogue actually filled in.

    A field that comes through empty for every camera is almost always a missing
    alias in `_FIELD_ALIASES` rather than a field the grid does not have, and
    that is a one-line fix — but only if someone notices it before onboarding.
    """
    print(f"\n{BOLD}Field coverage{RESET} {DIM}({len(cameras)} cameras){RESET}")
    total = len(cameras) or 1
    for name in REPORTED_FIELDS:
        present = sum(1 for c in cameras if getattr(c, name, None) not in (None, "", []))
        pct = 100 * present / total
        bar = "█" * round(pct / 5) or "·"
        colour = GREEN if pct > 90 else YELLOW if pct > 0 else RED
        note = ""
        if present == 0:
            note = f"  {DIM}← absent, or an alias is missing{RESET}"
        print(f"  {name:<14} {colour}{present:>3}/{total:<3}{RESET} {bar}{note}")


def report_estate(cameras: list[CatalogueCamera]) -> None:
    print(f"\n{BOLD}Estate{RESET}")

    def tally(values: list) -> str:
        counts: dict[str, int] = {}
        for v in values:
            counts[str(v) if v is not None else "unreported"] = (
                counts.get(str(v) if v is not None else "unreported", 0) + 1
            )
        return ", ".join(f"{k} ({n})" for k, n in sorted(counts.items(), key=lambda x: -x[1]))

    print(f"  transports    {tally([e.protocol for c in cameras for e in c.endpoints])}")
    print(f"  codecs        {tally([c.codec for c in cameras])}")
    print(f"  resolutions   {tally([c.resolution for c in cameras])}")
    print(f"  declared fps  {tally([c.declared_fps for c in cameras])}")
    print(f"  live flag     {tally([c.live for c in cameras])}")

    # The reference is explicit that the grid is not uniform. If it turns out to
    # be uniform after all, that is worth knowing too — it means a fixed-shape
    # batch would have worked, and we paid for flexibility we did not need.
    shapes = {c.resolution for c in cameras if c.resolution}
    if len(shapes) > 1:
        print(f"  {YELLOW}mixed resolutions confirmed — per-camera sizing is required{RESET}")

    placed = 0
    unplaced = []
    for c in cameras:
        if c.lat is not None and c.lon is not None:
            placed += 1
            continue
        place = resolve(c.location or c.name or "", hint=c.name)
        if place.precision != "unplaced":
            placed += 1
        else:
            unplaced.append(c.location or c.name or c.source_id)
    print(f"  positioned    {placed}/{len(cameras)}")
    if unplaced:
        print(f"  {YELLOW}no position for{RESET} {', '.join(unplaced[:6])}"
              f"{' …' if len(unplaced) > 6 else ''}")
        print(f"  {DIM}add these to data/gazetteer/gujarat.json{RESET}")


# --- the video ----------------------------------------------------------


def probe(camera: CatalogueCamera, seconds: float, *, plates: bool) -> dict:
    """Open one camera the way the worker would, and measure what arrives.

    Uses `LiveCapture`, so this exercises the real endpoint ladder, the real
    FFmpeg options and the real clock rather than a simplified stand-in. A
    result here that disagrees with the worker means the worker is wrong, which
    is the only way this is worth running.
    """
    from services.anpr.capture import LiveCapture, StreamCandidate

    candidates = [
        StreamCandidate(url=e.url, protocol=e.protocol) for e in camera.inference_endpoints
    ]
    if not candidates:
        return {"error": "no inference endpoint (rtsp/hls/http) in the catalogue"}

    locator = None
    if plates:
        try:
            from services.anpr.backends import build_locator

            locator = build_locator()
        except Exception as exc:  # noqa: BLE001 - a probe must not die on a model
            log.warning("plate locator unavailable, skipping crop measurement: %s", exc)

    stop_at = time.monotonic() + seconds
    widths: list[int] = []
    frames = 0
    first_shape = None
    capture = LiveCapture(candidates, label=camera.source_id)

    try:
        for frame in capture.frames():
            frames += 1
            if first_shape is None:
                first_shape = frame.pixels.shape[:2]
            # Every tenth frame: the locator is the expensive part and a crop
            # width is a property of the camera's geometry, not of this frame.
            if locator is not None and frames % 10 == 0:
                with contextlib.suppress(Exception):
                    widths.extend(d.box.width for d in locator.locate(frame.pixels))
            if time.monotonic() >= stop_at:
                break
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "frames": frames}

    stats = capture.stats
    height, width = first_shape or (None, None)
    return {
        "frames": frames,
        "endpoint": stats.endpoint,
        "protocol": stats.protocol,
        "clock": stats.clock,
        "declared_fps": camera.declared_fps,
        "measured_fps": frames / seconds if seconds else None,
        "decoded_width": width,
        "decoded_height": height,
        "discontinuities": stats.discontinuities,
        "failed_reads": stats.failed_reads,
        "plate_widths": widths,
    }


def report_probe(camera: CatalogueCamera, result: dict) -> None:
    label = f"{camera.source_id} · {(camera.name or '')[:34]}"
    print(f"\n{BOLD}{label}{RESET}")

    if "error" in result:
        print(f"  {RED}could not read{RESET}  {result['error']}")
        return

    print(f"  opened        {result['protocol']} {DIM}{result['endpoint']}{RESET}")
    print(f"  decoded       {result['decoded_width']}x{result['decoded_height']}, "
          f"{result['frames']} frames, {result['discontinuities']} discontinuities, "
          f"{result['failed_reads']} failed reads")

    # Question 2: is the declared rate the real one?
    declared, measured = result["declared_fps"], result["measured_fps"]
    clock = result["clock"]
    line = f"  clock         {clock}"
    if clock != "pts":
        line += f"  {YELLOW}← not PTS; timing fell back{RESET}"
    print(line)
    if measured is not None:
        note = ""
        if declared:
            drift = abs(measured - declared) / declared
            if drift > 0.15:
                note = (f"  {YELLOW}← declared {declared:g}, measured "
                        f"{measured:.1f} ({drift:.0%} out){RESET}")
        print(f"  frame rate    declared {declared or '?'}, "
              f"measured {measured:.1f}{note}")

    # Question 1: the one that decides the government-feed deliverable.
    widths = result["plate_widths"]
    if not widths:
        print(f"  plate crops   {DIM}none located in this window{RESET}")
        return
    median = statistics.median(widths)
    usable = sum(1 for w in widths if w >= USABLE_PLATE_PX)
    verdict = (f"{GREEN}ANPR-grade{RESET}" if median >= USABLE_PLATE_PX
               else f"{RED}below the {USABLE_PLATE_PX} px the OCR needs{RESET}")
    print(f"  plate crops   {len(widths)} located, median {median:.0f} px, "
          f"{usable}/{len(widths)} ≥ {USABLE_PLATE_PX} px — {verdict}")
    print(f"  {DIM}old estate for comparison: 66 px government / 276 px simulated{RESET}")


# --- waiting out an outage ----------------------------------------------


def wait_for_grid(interval_s: float) -> bool:
    """Block until the catalogue answers. Returns False if interrupted.

    The grid went to 502 on 31 Aug 2026 and nothing we do brings it back, so the
    only sensible posture is to be told the moment it returns rather than
    remembering to check. Polls politely — the reference says to pace load, and
    that applies to the control API too.

    Returns False and stops on an auth wall as well as on Ctrl-C. Later the same
    day the grid came back at a *third* host, `cctv.corp8.cloud`, answering 302
    to `/auth/login`. A loop that treated that as "still down" would poll a login
    page indefinitely while reporting the grid as unreachable, which is exactly
    the wrong thing to believe.
    """
    attempt = 0
    started = time.time()
    print(f"{BOLD}Waiting for the grid{RESET} {DIM}{BASE}{CATALOGUE_PATH}, "
          f"every {interval_s:g}s. Ctrl-C to stop.{RESET}")
    while True:
        attempt += 1
        try:
            cameras = fetch_catalogue(BASE)
        except CatalogueAuthRequired as exc:
            # Stop, rather than poll a login page until somebody notices. The
            # grid answering *is* the condition this loop waits for; it has
            # been met, and what is missing now is a credential, which will not
            # arrive by waiting.
            waited = time.time() - started
            print(f"\n{RED}{BOLD}The grid is back — behind a login{RESET} "
                  f"{DIM}after {waited / 60:.1f} min{RESET}")
            print(f"  {exc}")
            return False
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            reason = getattr(exc, "code", None) or type(exc).__name__
            waited = time.time() - started
            print(f"  {DIM}attempt {attempt:>3} · {waited / 60:5.1f} min · "
                  f"{reason}{RESET}", flush=True)
        else:
            waited = time.time() - started
            print(f"\n{GREEN}{BOLD}The grid is back{RESET} — {len(cameras)} cameras "
                  f"after {waited / 60:.1f} min. Running the full check.\n")
            return True
        try:
            time.sleep(interval_s)
        except KeyboardInterrupt:
            print(f"\n{DIM}stopped waiting{RESET}")
            return False


# --- main ---------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check the live grid without writing anything.",
    )
    parser.add_argument("--file", help="Read a saved catalogue response instead of the network.")
    parser.add_argument("--save", default="data/grid/catalogue-latest.json",
                        help="Where to write the raw catalogue response.")
    parser.add_argument("--cameras", type=int, default=2,
                        help="How many cameras to open and measure (0 for none).")
    parser.add_argument("--seconds", type=float, default=15.0,
                        help="How long to read each camera for.")
    parser.add_argument("--no-video", action="store_true",
                        help="Catalogue shape only; open no streams.")
    parser.add_argument("--no-plates", action="store_true",
                        help="Skip the plate-crop measurement (skips loading the locator).")
    parser.add_argument("--camera", action="append", default=[],
                        help="Probe this source id specifically. Repeatable.")
    parser.add_argument("--watch", type=float, nargs="?", const=300.0, default=None,
                        metavar="SECONDS",
                        help="Poll this often until the catalogue answers, then run "
                             "the full check. For waiting out an outage.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(name)s: %(message)s")

    if args.watch and not args.file and not wait_for_grid(args.watch):
        return 130

    source = args.file or f"{BASE}{CATALOGUE_PATH}"
    print(f"{BOLD}Grid check{RESET} {DIM}{source}{RESET}")

    try:
        cameras, payload = load(args)
    except CatalogueAuthRequired as exc:
        print(f"\n{RED}{BOLD}the grid is up, and we are not authorised{RESET}")
        print(f"  {exc}")
        print(f"{DIM}Nothing was written, and no amount of waiting fixes this — "
              f"`make grid-watch` would poll forever against a login page. "
              f"The next step is credentials, not code.{RESET}")
        return 2
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"\n{RED}catalogue unreachable{RESET}  {type(exc).__name__}: {exc}")
        print(f"{DIM}Nothing was written. If the grid is down, run `make grid-watch` "
              f"to be told when it returns.{RESET}")
        return 1

    if not cameras:
        print(f"\n{RED}the catalogue parsed but contained no cameras{RESET}")
        print(f"{DIM}Save the response and check the list key — see _LIST_KEYS in "
              f"services/adapters/catalogue.py{RESET}")
        return 1

    if not args.file and args.save:
        os.makedirs(os.path.dirname(args.save) or ".", exist_ok=True)
        with open(args.save, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        print(f"{DIM}raw response saved to {args.save} — "
              f"re-run offline with --file {args.save}{RESET}")

    report_coverage(cameras)
    report_estate(cameras)

    if args.no_video or args.cameras <= 0:
        print(f"\n{DIM}Streams not opened (--no-video). "
              f"The plate-crop question is unanswered.{RESET}")
        return 0

    if args.camera:
        wanted = [c for c in cameras if c.source_id in set(args.camera)]
    else:
        # Prefer cameras the catalogue believes are live; a probe of a camera
        # the grid already says is down measures nothing.
        wanted = [c for c in cameras if c.live is not False][: args.cameras]

    print(f"\n{BOLD}Opening {len(wanted)} camera(s) for {args.seconds:g}s each{RESET}")
    results = {}
    for camera in wanted:
        result = probe(camera, args.seconds, plates=not args.no_plates)
        results[camera.source_id] = result
        report_probe(camera, result)

    read_ok = [r for r in results.values() if "error" not in r]
    print(f"\n{BOLD}Summary{RESET}")
    print(f"  catalogue parsed            {_ok(True)} ({len(cameras)} cameras)")
    print(f"  streams opened              {_ok(bool(read_ok))} "
          f"({len(read_ok)}/{len(results)})")
    print(f"  timing from PTS             "
          f"{_ok(any(r.get('clock') == 'pts' for r in read_ok))}")
    crops = [w for r in read_ok for w in r.get("plate_widths", [])]
    if crops:
        median = statistics.median(crops)
        print(f"  plate crops ANPR-grade      {_ok(median >= USABLE_PLATE_PX)} "
              f"(median {median:.0f} px)")
    else:
        print(f"  plate crops ANPR-grade      {DIM}not measured{RESET}")

    print(f"\n{DIM}Nothing was written to the registry. "
          f"Next: `make onboard-dry`, then `make onboard`.{RESET}")
    return 0 if read_ok else 1


if __name__ == "__main__":
    sys.exit(main())
