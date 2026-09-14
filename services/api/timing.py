"""Request latency, measured rather than claimed.

docs/build-plan.md section 1.3 is explicit that "evidence of end-to-end system
performance" is an artifact, not a sentence, and one of the figures it names is
p95 query latency. Nothing in the platform was measuring it: M4's 26 ms came
from the acceptance test timing five requests from outside, which proves the
journey query and nothing else.

This records every request in process, keyed by the route template rather than
the path, so ten thousand journey queries for ten thousand plates aggregate into
one line instead of ten thousand. Percentiles come from a bounded ring per
route: a few hundred kilobytes for the whole API, and no dependency on a metrics
backend that would have to be running for the demo to have numbers.

What this is not. It measures the API's own handling time, not what a browser
experiences: no network, no render. Reporting it as the user-visible latency
would be the kind of flattering figure the honesty convention rules out, so the
performance page says which one it is.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware

__all__ = ["TimingMiddleware", "snapshot", "SAMPLES_PER_ROUTE"]

#: Samples kept per route. Enough that a p95 means something, small enough that
#: a hundred routes cost about a megabyte.
SAMPLES_PER_ROUTE = 512


@dataclass
class _Route:
    durations: deque[float] = field(
        default_factory=lambda: deque(maxlen=SAMPLES_PER_ROUTE)
    )
    requests: int = 0
    errors: int = 0
    slowest_ms: float = 0.0


_routes: dict[str, _Route] = defaultdict(_Route)
_lock = Lock()


def record(route: str, ms: float, status: int) -> None:
    with _lock:
        entry = _routes[route]
        entry.durations.append(ms)
        entry.requests += 1
        entry.slowest_ms = max(entry.slowest_ms, ms)
        if status >= 500:
            entry.errors += 1


def _percentile(ordered: list[float], fraction: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def snapshot() -> list[dict[str, Any]]:
    """Per-route latency, busiest first."""
    with _lock:
        rows = [
            (route, list(entry.durations), entry.requests, entry.errors, entry.slowest_ms)
            for route, entry in _routes.items()
        ]

    out = []
    for route, durations, requests, errors, slowest in rows:
        ordered = sorted(durations)
        out.append({
            "route": route,
            "requests": requests,
            "errors": errors,
            "samples": len(ordered),
            "p50_ms": round(_percentile(ordered, 0.50), 2),
            "p95_ms": round(_percentile(ordered, 0.95), 2),
            "max_ms": round(slowest, 2),
        })
    return sorted(out, key=lambda r: r["requests"], reverse=True)


class TimingMiddleware(BaseHTTPMiddleware):
    """Times every request and returns the figure in a header.

    The header is there so a client can show the server's own view beside its
    measured round trip. The difference between the two is the network, which is
    exactly the thing an evaluator on a hosted instance will want to see
    separated out.
    """

    async def dispatch(self, request: Any, call_next: Any) -> Any:
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000

        # The route template ("/api/vehicles/{plate}/journey"), not the path.
        # Falls back to the raw path only for a 404, which has no route.
        route = request.scope.get("route")
        template = getattr(route, "path", None) or request.url.path
        record(f"{request.method} {template}", elapsed_ms, response.status_code)

        response.headers["X-Response-Time-Ms"] = f"{elapsed_ms:.2f}"
        return response
