"""Per-stage timing, rolled up in process.

The build plan is explicit that M3 must be instrumented as it is built, because
these numbers become the M7 performance evidence and the HLD sizing figures,
and retrofitting instrumentation is wasted work.

Rolled up rather than sampled per frame: a row per frame per stage would
generate more write traffic than the sightings do, and would mostly measure the
instrumentation. One row per camera per stage per minute keeps the percentiles
that matter at a cost that disappears next to the detector.

Percentiles are computed from the samples held for the current window, so they
are exact for that window rather than an estimate — the windows are small
enough that keeping them is cheaper than approximating them.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

__all__ = ["StageStats", "MetricsCollector", "percentile"]


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile. Exact for the window, not an approximation."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(round(pct / 100.0 * len(ordered) + 0.5))))
    return ordered[rank - 1]


@dataclass
class StageStats:
    stage: str
    samples: int
    p50_ms: float
    p95_ms: float
    max_ms: float


@dataclass
class MetricsCollector:
    """Timings and counters for one camera, for the current rollup window."""

    camera_id: str
    window_s: float = 60.0
    started_at: float = field(default_factory=time.monotonic)
    timings: dict[str, list[float]] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Time one pipeline stage."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self.timings.setdefault(name, []).append((time.perf_counter() - start) * 1000.0)

    def record(self, stage: str, duration_ms: float) -> None:
        self.timings.setdefault(stage, []).append(duration_ms)

    def count(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + n

    def due(self, now: float | None = None) -> bool:
        return (now or time.monotonic()) - self.started_at >= self.window_s

    def snapshot(self) -> list[StageStats]:
        return [
            StageStats(
                stage=stage,
                samples=len(values),
                p50_ms=round(percentile(values, 50), 3),
                p95_ms=round(percentile(values, 95), 3),
                max_ms=round(max(values), 3),
            )
            for stage, values in sorted(self.timings.items())
            if values
        ]

    def reset(self, now: float | None = None) -> None:
        self.started_at = now or time.monotonic()
        self.timings.clear()
        self.counters.clear()
