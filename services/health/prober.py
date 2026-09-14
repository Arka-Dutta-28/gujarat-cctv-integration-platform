"""Camera health prober.

Polls every registered camera, decides a status (services/health/status.py) and
writes both the current state onto `cameras` and a history row into
`camera_health`. The map's colours and M7's uptime figures both come from here.

Three probe strategies, chosen per camera from its stream_ref:

    MediaMTX control API   for anything published to our own media server. One
                           HTTP call covers all 50 simulated cameras, so the
                           common case costs one request per cycle, not fifty.
    RTSP OPTIONS           over a raw socket, for a direct camera URL. Enough to
                           prove something is listening and speaking RTSP,
                           without pulling video or needing ffmpeg in this image.
    HTTP HEAD or GET       for an HLS or HTTP-delivered feed.

The last two exist because the government feeds are served from their own
endpoints, and the platform must be able to say whether one is up without
routing it through our media server first.

Usage:
    python -m services.health.prober                # run continuously
    python -m services.health.prober --once         # single cycle, then exit
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import psycopg

from services.adapters import get_adapter
from services.common.config import settings
from services.common.db import wait_for_db
from services.health.status import CameraStatus, HealthVerdict, StreamSample, assess

log = logging.getLogger("prober")

# How often to sweep. The M1 acceptance test requires a newly added camera to
# turn green promptly, so this is tuned for demo responsiveness rather than for
# minimising load — at one API call per cycle it is cheap either way.
DEFAULT_INTERVAL_S = 5.0

PROBE_TIMEOUT_S = 4.0

# Bitrate expectations are learned per camera rather than configured. A static
# expectation would be wrong for every real camera, whose bitrate depends on
# scene content; a camera's own recent history is a far better baseline.
EWMA_ALPHA = 0.25
MIN_SAMPLES_BEFORE_BITRATE_JUDGEMENT = 5

# Consecutive cycles a non-online verdict must repeat before it is committed.
#
# Without this the farm's deliberately-degraded camera flaps: at 4 fps a
# five-second sample window either straddles a keyframe or does not, so its
# bitrate legitimately swings by 3-4x and the learned baseline flags every dip.
# Flapping status is worse than a slightly stale one — it destroys the operator's
# trust in the map and corrupts the uptime figures M7 reports.
#
# Only degradations are debounced. Recovery to online is applied immediately, so
# a camera that is genuinely back shows green at once and a newly onboarded
# camera turns green on its first successful probe (M1 acceptance).
DEGRADE_CONFIRMATIONS = 3

# Cameras we do not proxy are probed over the network one connection each, so
# they are latency-bound rather than CPU-bound. Probing them serially made a
# 5-second sweep of 81 cameras take 19 seconds, which both breaks the "a new
# camera turns green quickly" requirement and scales nowhere near the ~80,000
# cameras the platform is meant to be sized for. Fan them out instead.
DIRECT_PROBE_WORKERS = 32


@dataclass
class CameraRow:
    id: str
    external_ref: str | None
    name: str
    stream_ref: str
    adapter: str
    status: str


@dataclass
class _Track:
    """Rolling per-camera observation state, held in memory between cycles."""

    last_bytes: int | None = None
    last_seen_bytes_at: float | None = None
    last_change_at: float | None = None
    ewma_bitrate: float | None = None
    samples: int = 0
    #: Verdict awaiting confirmation, and how many cycles it has held.
    pending_status: str | None = None
    pending_count: int = 0

    def observe_bytes(self, total: int, now: float) -> tuple[float | None, float | None]:
        """Fold in a byte counter, returning (bitrate_bps, seconds_since_data)."""
        bitrate: float | None = None
        if self.last_bytes is not None and self.last_seen_bytes_at is not None:
            dt = now - self.last_seen_bytes_at
            delta = total - self.last_bytes
            if dt > 0 and delta >= 0:
                bitrate = delta * 8 / dt
            if delta > 0:
                self.last_change_at = now
        else:
            self.last_change_at = now

        self.last_bytes = total
        self.last_seen_bytes_at = now

        since = (now - self.last_change_at) if self.last_change_at is not None else None
        return bitrate, since

    def expectation(self, bitrate: float | None) -> float | None:
        """Update and return the learned bitrate baseline for this camera."""
        if bitrate is None or bitrate <= 0:
            if self.samples >= MIN_SAMPLES_BEFORE_BITRATE_JUDGEMENT:
                return self.ewma_bitrate
            return None

        baseline = (
            self.ewma_bitrate
            if self.samples >= MIN_SAMPLES_BEFORE_BITRATE_JUDGEMENT
            else None
        )
        self.ewma_bitrate = (
            bitrate if self.ewma_bitrate is None
            else EWMA_ALPHA * bitrate + (1 - EWMA_ALPHA) * self.ewma_bitrate
        )
        self.samples += 1
        return baseline


def fetch_cameras(conn: psycopg.Connection) -> list[CameraRow]:
    rows = conn.execute(
        """
        SELECT id::text, external_ref, name, stream_ref, adapter::text, status::text
          FROM cameras
         WHERE status <> 'decommissioned'
         ORDER BY external_ref
        """
    ).fetchall()
    return [CameraRow(*r) for r in rows]


def fetch_mediamtx_paths(api_base: str) -> tuple[dict[str, Any], str | None]:
    """All path state in one call. Returns (by-name mapping, error)."""
    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed internal URL
            f"{api_base}/v3/paths/list?itemsPerPage=1000", timeout=PROBE_TIMEOUT_S
        ) as resp:
            data = json.load(resp)
        return {i["name"]: i for i in data.get("items", [])}, None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return {}, str(exc)[:200]


def probe_rtsp(url: str, timeout: float = PROBE_TIMEOUT_S) -> StreamSample:
    """Liveness via an RTSP OPTIONS handshake — no video pulled, no ffmpeg."""
    parsed = urlparse(url)
    host, port = parsed.hostname, parsed.port or 554
    if not host:
        return StreamSample(ready=False, probe_failed=True, error=f"unparseable URL: {url}")

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            # Credentials are never placed in this request line; the registry
            # holds a credential_ref, and auth is negotiated by the adapter.
            safe = parsed._replace(netloc=f"{host}:{port}").geturl()
            request = (
                f"OPTIONS {safe} RTSP/1.0\r\n"
                "CSeq: 1\r\n"
                "User-Agent: cctv-platform-prober\r\n\r\n"
            )
            sock.sendall(request.encode())
            reply = sock.recv(1024).decode(errors="replace")
    except OSError as exc:
        return StreamSample(ready=False, error=f"{type(exc).__name__}: {exc}")

    if "RTSP/1.0 200" in reply:
        return StreamSample(ready=True)
    if "RTSP/1.0 401" in reply or "RTSP/1.0 403" in reply:
        # Answering at all proves the camera is alive; we simply may not view it.
        return StreamSample(ready=True, error="authentication required")
    first_line = reply.split("\r\n", 1)[0] if reply else "(no reply)"
    return StreamSample(ready=False, error=f"unexpected RTSP reply: {first_line}")


#: Content types that prove a 200 is not video, however healthy it looks. An
#: HTTP camera sitting behind a sign-in redirect answers every probe with a
#: page, and a probe that only checks the status code believes it.
#:
#: Deliberately only HTML. `text/plain` is tempting to add and would be a bug:
#: plenty of servers hand out an HLS playlist as text/plain, and rejecting it
#: would black out working cameras in order to catch a login page.
NON_MEDIA_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})


def probe_http(url: str, timeout: float = PROBE_TIMEOUT_S) -> StreamSample:
    """Liveness for an HLS or HTTP-delivered feed.

    Asks for a single byte. That detail is load-bearing: a progressive endpoint
    serves an unbounded stream, so a plain GET never returns and would hang the
    prober on the first such camera. A Range request gets the status line and a
    few bytes, which is all a liveness check needs.

    A 2xx alone is not accepted as healthy. urllib follows redirects, so a feed
    that has been moved behind a sign-in answers 200 — with a login page. The
    content type is what separates the two.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "cctv-platform-prober", "Range": "bytes=0-0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            resp.read(1)
            if not 200 <= resp.status < 300:
                return StreamSample(ready=False, error=f"HTTP {resp.status}")
            kind = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if kind in NON_MEDIA_CONTENT_TYPES:
                # A 200 is not enough. Observed 1 Sep 2026: the grid's old
                # hostname 301s to a login page, which answers 200 with
                # `text/html` — and the prober called thirty cameras
                # "streaming" while every one of them was serving a form.
                # A green pin over a captive portal is worse than a red one:
                # it sends an operator to a camera that cannot be watched.
                return StreamSample(
                    ready=False,
                    error=f"served {kind}, not media (login page or error page?)",
                )
            return StreamSample(ready=True)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # Answering at all proves it is alive; we may simply not view it.
            return StreamSample(ready=True, error="authentication required")
        if exc.code >= 500:
            # An upstream that cannot serve the stream is offline to us, whatever
            # its own status field claims. Two of the 31 evaluation feeds report
            # "live" while returning 500 on every request, so believing the
            # upstream's self-assessment would hide a genuinely dead camera.
            return StreamSample(ready=False, error=f"upstream error HTTP {exc.code}")
        return StreamSample(ready=False, error=f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return StreamSample(ready=False, error=f"{type(exc).__name__}: {exc}")


class Prober:
    def __init__(self, dsn: str, mediamtx_api: str, interval_s: float) -> None:
        self.dsn = dsn
        self.mediamtx_api = mediamtx_api
        self.interval_s = interval_s
        self.tracks: dict[str, _Track] = {}
        self._stop = threading.Event()

    @staticmethod
    def _is_proxied(cam: CameraRow) -> bool:
        """True when this camera is published to our own media server."""
        parsed = urlparse(cam.stream_ref)
        return parsed.scheme in {"rtsp", "rtsps"} and parsed.hostname in {
            settings.mediamtx_host, "localhost", "127.0.0.1", "mediamtx",
        }

    def _sample_for(
        self, cam: CameraRow, paths: dict[str, Any], paths_error: str | None, now: float
    ) -> StreamSample:
        parsed = urlparse(cam.stream_ref)

        if self._is_proxied(cam):
            if paths_error is not None:
                return StreamSample(ready=False, probe_failed=True, error=paths_error)

            info = paths.get(parsed.path.lstrip("/"))
            if info is None:
                return StreamSample(ready=False, error="no such path on the media server")

            track = self.tracks.setdefault(cam.id, _Track())
            bitrate, since = track.observe_bytes(int(info.get("bytesReceived", 0)), now)
            expected = track.expectation(bitrate)
            return StreamSample(
                ready=bool(info.get("ready")),
                seconds_since_data=since,
                bitrate_bps=bitrate,
                expected_bitrate_bps=expected,
            )

        # A camera we do not proxy: probe it where it lives.
        #
        # Whether a source is probeable at all is the adapter's declaration, not
        # a name this module knows. Branching on `adapter == "file"` here would
        # mean every new adapter type needed an edit in the health prober, which
        # is exactly what the adapter spine exists to prevent.
        try:
            if not get_adapter(cam.adapter).probeable:
                return StreamSample(ready=True, error=f"{cam.adapter} source, not probed")
        except LookupError:
            return StreamSample(
                ready=False, probe_failed=True, error=f"no adapter for {cam.adapter!r}"
            )

        if parsed.scheme in {"rtsp", "rtsps"}:
            return probe_rtsp(cam.stream_ref)
        if parsed.scheme in {"http", "https"}:
            return probe_http(cam.stream_ref)
        return StreamSample(
            ready=False, probe_failed=True, error=f"no probe for scheme {parsed.scheme!r}"
        )

    def _debounce(self, cam: CameraRow, verdict: HealthVerdict) -> HealthVerdict:
        """Hold a degradation until it repeats; let recovery through at once."""
        track = self.tracks.setdefault(cam.id, _Track())

        if verdict.status == CameraStatus.ONLINE:
            track.pending_status = None
            track.pending_count = 0
            return verdict

        if track.pending_status == verdict.status:
            track.pending_count += 1
        else:
            track.pending_status = verdict.status
            track.pending_count = 1

        if track.pending_count >= DEGRADE_CONFIRMATIONS:
            return verdict

        # Not yet confirmed. Keep reporting what the camera currently is, rather
        # than committing to a state one noisy sample suggested.
        return HealthVerdict(
            CameraStatus(cam.status),
            f"{verdict.reason} (unconfirmed, {track.pending_count}/{DEGRADE_CONFIRMATIONS})",
            verdict.clock_suspect,
        )

    def cycle(self) -> dict[str, int]:
        """One sweep. Returns a status histogram."""
        now = time.monotonic()
        paths, paths_error = fetch_mediamtx_paths(self.mediamtx_api)
        if paths_error:
            log.warning("MediaMTX API unreachable: %s", paths_error)

        histogram: dict[str, int] = {}
        with psycopg.connect(self.dsn) as conn:
            cameras = fetch_cameras(conn)

            # Media-server cameras resolve from the single API response already
            # in hand; the rest each need their own network round trip, so fan
            # those out rather than paying for them one after another.
            proxied = [c for c in cameras if self._is_proxied(c)]
            direct = [c for c in cameras if not self._is_proxied(c)]

            samples: dict[str, StreamSample] = {
                cam.id: self._sample_for(cam, paths, paths_error, now) for cam in proxied
            }
            if direct:
                with ThreadPoolExecutor(
                    max_workers=min(DIRECT_PROBE_WORKERS, len(direct))
                ) as pool:
                    for cam, sample in zip(
                        direct,
                        pool.map(lambda c: self._sample_for(c, paths, paths_error, now), direct),
                        strict=True,
                    ):
                        samples[cam.id] = sample

            updates: list[tuple] = []
            history: list[tuple] = []

            for cam in cameras:
                sample = samples[cam.id]
                verdict: HealthVerdict = assess(sample)
                verdict = self._debounce(cam, verdict)
                histogram[verdict.status] = histogram.get(verdict.status, 0) + 1

                if verdict.status != cam.status:
                    log.info(
                        "%s %s -> %s (%s)",
                        cam.external_ref or cam.id, cam.status,
                        verdict.status, verdict.reason,
                    )

                updates.append((verdict.status, verdict.status == CameraStatus.ONLINE, cam.id))
                history.append(
                    (
                        cam.id,
                        verdict.status,
                        None,
                        None,
                        int(sample.seconds_since_data)
                        if sample.seconds_since_data is not None else None,
                        False,
                    )
                )

            with conn.cursor() as cur:
                # last_seen only advances while the camera is actually online, so
                # it stays meaningful as "when we last had pictures".
                # The decommissioned guard is repeated here, not just in the
                # read. A sweep takes a few seconds, so a camera retired midway
                # through one would otherwise be resurrected by this write —
                # the prober would silently undo an operator's decision.
                cur.executemany(
                    "UPDATE cameras SET status = %s::camera_status,"
                    " last_seen = CASE WHEN %s THEN now() ELSE last_seen END"
                    " WHERE id = %s AND status <> 'decommissioned'",
                    updates,
                )
                cur.executemany(
                    "INSERT INTO camera_health"
                    " (ts, camera_id, status, fps, latency_ms, last_frame_age_s, tamper)"
                    " VALUES (now(), %s, %s::camera_status, %s, %s, %s, %s)",
                    history,
                )
            conn.commit()
        return histogram

    def run(self, once: bool = False) -> int:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                histogram = self.cycle()
                log.info(
                    "probed %d cameras: %s",
                    sum(histogram.values()),
                    ", ".join(f"{k}={v}" for k, v in sorted(histogram.items())) or "none",
                )
            except Exception:  # noqa: BLE001 - one bad cycle must not kill the prober
                log.exception("probe cycle failed; retrying next interval")

            if once:
                return 0
            self._stop.wait(max(0.5, self.interval_s - (time.monotonic() - started)))
        return 0

    def request_stop(self, *_: object) -> None:
        self._stop.set()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description="Probe camera health.")
    parser.add_argument("--once", action="store_true", help="single cycle, then exit")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    args = parser.parse_args()

    wait_for_db()
    api = f"http://{settings.mediamtx_host}:{settings.mediamtx_api_port}"
    prober = Prober(settings.dsn, api, args.interval)
    signal.signal(signal.SIGTERM, prober.request_stop)
    signal.signal(signal.SIGINT, prober.request_stop)
    return prober.run(once=args.once)


if __name__ == "__main__":
    sys.exit(main())
