"""Run the ANPR pipeline over one video file and print what it read.

A debugging and demonstration tool, deliberately separate from the worker: it
touches no database, no registry and no network, so when plates are not being
read it answers the only question that matters first — *is it the pipeline or is
it the plumbing?*

It also prints the per-stage timings and the sampler's saving for that clip,
which is the same instrumentation the worker writes to `anpr_stage_stats`, so a
figure seen here should match one seen in `/api/performance`.

Usage:
    python -m scripts.anpr_probe data/test-videos/traffic-08-day.mp4
    python -m scripts.anpr_probe rtsp://mediamtx:8554/cam-01 --seconds 20
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from services.anpr.capture import PtsClock
from services.anpr.conditions import classify
from services.anpr.metrics import MetricsCollector
from services.anpr.pipeline import AnprPipeline, FrameContext
from services.anpr.sampling import AdaptiveSampler
from services.anpr.tracks import TrackRegistry
from services.anpr.worker import FrameAnalyser, _build_locator, _build_ocr, _build_tracker

GREEN, DIM, BOLD, RESET = "\033[32m", "\033[2m", "\033[1m", "\033[0m"


def _print_descriptions(results: list, read: list) -> None:
    """What the run could say about vehicles it could not read.

    Printed next to the read rate on purpose. On a camera below ANPR grade the
    read rate is the whole story and it is close to zero, and this is the line
    that says whether the feed is useless or merely unreadable — which are very
    different things for an operator and for a report.
    """
    if not results:
        return
    described = [d for d in results if d.track.vehicle_colour]
    unread_described = [d for d in described if not d.result]
    print(
        f"{DIM}{len(described)} described ({len(described) / len(results):.0%}), "
        f"of which {len(unread_described)} carry a description and no plate — "
        f"rows that would not exist without it{RESET}"
    )
    # Coverage of the re-ID descriptor, which is the half of this that used to
    # be invisible. Until 6 Sep 2026 it was computed only on the frame that
    # read a plate best, so this number was exactly the read count — and on a
    # camera that reads nothing it was zero, which is where re-ID was needed
    # most.
    embedded = [d for d in results if d.track.embedding]
    print(
        f"{DIM}{len(embedded)} carry a re-ID descriptor "
        f"({len(embedded) / len(results):.0%}); before the fix this was the "
        f"read count, {len(read)}{RESET}"
    )
    if not described:
        return

    counts: dict[str, int] = {}
    for done in described:
        colour = done.track.vehicle_colour or "?"
        counts[colour] = counts.get(colour, 0) + 1
    spread = "  ".join(
        f"{name} {n}" for name, n in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    print(f"{DIM}colours: {spread}{RESET}")
    confidences = sorted(d.track.colour_confidence for d in described)
    median = confidences[len(confidences) // 2]
    print(f"{DIM}median colour confidence {median:.2f}{RESET}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run ANPR over one source.")
    parser.add_argument("source", help="Video file, or any URL OpenCV can open.")
    parser.add_argument("--seconds", type=float, default=0, help="Stop after this long.")
    parser.add_argument("--every-frame", action="store_true",
                        help="Disable adaptive sampling, for a like-for-like timing run.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(name)s: %(message)s")

    import cv2

    capture = cv2.VideoCapture(args.source)
    if not capture.isOpened():
        print(f"could not open {args.source}", file=sys.stderr)
        return 1

    metrics = MetricsCollector("probe")
    sampler = AdaptiveSampler()
    if args.every_frame:
        from services.anpr.sampling import SamplerConfig

        sampler = AdaptiveSampler(config=SamplerConfig(max_hz=10_000, min_hz=10_000))

    pipeline = AnprPipeline(
        camera_id="probe",
        tracker=_build_tracker(),
        locator=_build_locator(),
        ocr=_build_ocr(),
        sampler=sampler,
        registry=TrackRegistry("probe"),
        metrics=metrics,
    )

    analyser = FrameAnalyser()
    started = time.monotonic()
    results = []

    # A file decodes far faster than it plays, so wall time is the wrong clock:
    # the sampler would see ninety seconds of video arrive in four and analyse a
    # handful of frames. Media time makes a probe behave like the live stream it
    # stands in for.
    #
    # Media time comes from **PTS**, through the same clock the worker uses. The
    # earlier version computed it as `frame_index / CAP_PROP_FPS`, which
    # combines the two things the integration reference names explicitly: it
    # trusts the declared frame rate, and it assumes a constant one. On a stream
    # where either is wrong, every dwell and sampling decision the probe reports
    # is wrong with it — and a measurement harness that measures the wrong thing
    # is worse than no harness, because its numbers get quoted.
    clock = PtsClock()

    while True:
        ok, frame = capture.read()
        if not ok:
            break
        timed = clock.observe(
            capture.get(cv2.CAP_PROP_POS_MSEC), time.time(), time.monotonic()
        )
        now = timed.monotonic_s
        if args.seconds and now > args.seconds:
            break

        stats = analyser.analyse(frame)
        for done in pipeline.process(
            frame,
            FrameContext(
                now=now, motion=stats.motion,
                condition=classify(stats.mean_luma, stats.bright_fraction),
            ),
        ):
            results.append(done)

    results.extend(pipeline.flush())
    capture.release()
    elapsed = time.monotonic() - started

    read = [r for r in results if r.result]
    print(f"\n{BOLD}{args.source}{RESET}")
    decoded = metrics.counters.get("frames_decoded", 0)
    analysed = metrics.counters.get("frames_analysed", 0)
    # Which clock the run was on is part of the result, not a footnote: a probe
    # that fell back to arrival time produces weaker dwell figures, and a reader
    # comparing two probes needs to know whether they were measured the same way.
    print(f"{DIM}clock: {clock.source} · {timed.monotonic_s:.1f}s of video · "
          f"{elapsed:.1f}s wall · {decoded} frames decoded · "
          f"{analysed} analysed ({sampler.analysed_fraction:.1%}){RESET}\n")

    print(f"{BOLD}{'plate':<14} {'conf':>5} {'reads':>6} {'agree':>6} {'condition':<8} "
          f"{'valid':>6}  source{RESET}")
    for done in sorted(read, key=lambda d: -(d.result.confidence if d.result else 0)):
        v = done.result
        assert v is not None
        mark = f"{GREEN}yes{RESET}" if v.format_valid else " no"
        how = "consensus" if v.from_consensus else "majority"
        print(f"{v.plate.pretty:<14} {v.confidence:>5.2f} {v.read_count:>6} "
              f"{v.agreement:>6.2f} {done.track.condition or '?':<8} {mark:>6}  {how}")

    tracked = len(results)
    if tracked:
        print(f"\n{DIM}{tracked} vehicles tracked, {len(read)} produced a plate "
              f"({len(read) / tracked:.0%} read rate){RESET}")
    else:
        print(f"\n{DIM}no vehicles tracked{RESET}")

    _print_descriptions(results, read)

    print(f"\n{BOLD}stage timings (ms){RESET}")
    for s in metrics.snapshot():
        print(f"  {s.stage:<16} p50 {s.p50_ms:>7.2f}  p95 {s.p95_ms:>7.2f}  "
              f"max {s.max_ms:>7.2f}  n={s.samples}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
