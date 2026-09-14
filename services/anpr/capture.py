"""Live-stream capture: one camera, read the way a live camera must be read.

Everything in this module exists because the grid is live RTP/RTSP, not a file.
The integration reference for the Sentinel camera grid
(docs/integration-contract.md) states the behaviour of that transport, and each
rule below maps to a clause of it. They are collected here, in one place, rather
than sprinkled through the worker, so that "does the pipeline consume the grid
correctly?" is a question about a single module with its own tests.

Transport is forced to TCP. UDP is accepted by the gateway, but partial delivery
across NAT produces torn frames that look exactly like model bugs. The option
has to be set through FFmpeg's capture-options environment variable because
OpenCV exposes no API for it, and it is read at VideoCapture construction, so it
is set around the open and restored after. See _opened.

Timing comes from PTS, never from arrival time (invariant 7). The gateway
replays its buffered group-of-pictures on connect so the decoder can start at a
keyframe, which means the first second or two of frames arrive faster than real
time. A tracker stamping frames on arrival computes impossible velocities on
every single connection, and the journey-plausibility check (over 120 km/h
splits a journey) would then fire on its own client. CAP_PROP_POS_MSEC is the
clock.

The declared frame rate is not used for anything. CAP_PROP_FPS is frequently
wrong on these streams, and any metric derived from it (dwell time, speed,
frames to seconds) inherits the error. The real rate is measured, and it is
measured only to be reported.

Frame intervals are not assumed uniform. Everything downstream is driven by
elapsed PTS between frames, so a gap is a longer delta and nothing else. A gap
is not a disconnect.

Decoder complaints at join are not failures. Attaching mid-stream to H.265
produces "Error constructing the frame RPS" and similar until the first IDR
arrives. A pipeline that aborts on the first bad read bounces on those streams
forever, so failed reads are tolerated: generously during the join window, less
so afterwards.

Reconnect is exponential with jitter, never a tight loop. Feeds are supervised
and restart.

A loop point is a scene discontinuity, not a new world (invariant 8). Each feed
is a recording that loops; at the cut the scene changes completely, like a
camera reboot. Long-lived state, meaning background models, tracker ids, re-ID
galleries and the tamper reference, must be told, or it carries the previous
scene's beliefs into a different one. Frame.discontinuity is that signal.

The endpoint ladder is data, not a constant. A camera may be reachable over
RTSP, or only over HLS if 8554 is blocked on this network, or only through the
relay. Candidates come from the caller, ultimately from the registry, which is
fed by the catalogue, and this module remembers which one worked.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("anpr.capture")

__all__ = [
    "StreamCandidate",
    "Frame",
    "Backoff",
    "PtsClock",
    "LiveCapture",
    "CaptureStats",
    "ffmpeg_options_for",
]

# --- reconnect policy ---------------------------------------------------
# "start at ~2 s, cap at ~30 s" — from the integration reference.

BACKOFF_BASE_S = float(os.environ.get("CAPTURE_BACKOFF_BASE_S", "2.0"))
BACKOFF_CAP_S = float(os.environ.get("CAPTURE_BACKOFF_CAP_S", "30.0"))

#: A session that delivered frames for at least this long counts as a success,
#: so the backoff resets. Shorter than this and the stream is flapping; keeping
#: the backoff growing is the point.
SESSION_HEALTHY_S = float(os.environ.get("CAPTURE_HEALTHY_SESSION_S", "30.0"))

# --- join tolerance -----------------------------------------------------

#: How long after connecting decoder complaints are expected rather than
#: alarming. The first IDR usually arrives well inside this.
JOIN_GRACE_S = float(os.environ.get("CAPTURE_JOIN_GRACE_S", "10.0"))

#: Failed reads tolerated during the join window before giving up on it.
JOIN_GRACE_READS = int(os.environ.get("CAPTURE_JOIN_GRACE_READS", "150"))

#: Failed reads tolerated once the stream has been delivering. Above zero
#: because a supervised feed can hiccup without being gone.
STEADY_GRACE_READS = int(os.environ.get("CAPTURE_STEADY_GRACE_READS", "20"))

#: Pause between retries of a failed read, so a dead socket does not spin a
#: core. Small: a live stream that is merely between frames must not be delayed.
FAILED_READ_PAUSE_S = 0.02

#: No frame for this long is a dead stream, whatever the socket believes.
#: Distinct from an inter-frame gap, which is normal and simply larger than the
#: nominal interval.
STALL_TIMEOUT_S = float(os.environ.get("CAPTURE_STALL_TIMEOUT_S", "30.0"))

# --- discontinuity ------------------------------------------------------

#: PTS running backwards by more than this is the loop point. A small negative
#: delta is B-frame reordering or a rounding artefact, not a cut.
PTS_REGRESSION_S = float(os.environ.get("CAPTURE_PTS_REGRESSION_S", "1.0"))

#: Media time advancing faster than this multiple of wall time means the
#: buffered group-of-pictures is still draining. 1.25 rather than 1.0 so that
#: ordinary jitter in the measurement does not read as a burst.
CATCHUP_RATE = float(os.environ.get("CAPTURE_CATCHUP_RATE", "1.25"))

#: Window over which that rate is measured. Long enough to be stable, short
#: enough that a burst of a second or two is still visible in it.
RATE_WINDOW_S = float(os.environ.get("CAPTURE_RATE_WINDOW_S", "0.5"))

#: PTS jumping forward by more than this is also a discontinuity — a gateway
#: restart, or a seek upstream. Well above any plausible inter-frame gap.
PTS_JUMP_S = float(os.environ.get("CAPTURE_PTS_JUMP_S", "60.0"))

# --- socket options -----------------------------------------------------

#: FFmpeg socket timeout, microseconds. Without it a half-open TCP connection
#: blocks `read()` indefinitely and the camera silently stops being processed.
FFMPEG_TIMEOUT_US = int(float(os.environ.get("CAPTURE_SOCKET_TIMEOUT_S", "15.0")) * 1_000_000)

#: `OPENCV_FFMPEG_CAPTURE_OPTIONS` is process-global and consumed during
#: `VideoCapture` construction, so two threads opening different cameras at the
#: same moment can read each other's options. One lock, held only across the
#: constructor.
_open_lock = threading.Lock()

_RTSP_SCHEMES = {"rtsp", "rtsps"}


def ffmpeg_options_for(url: str) -> str:
    """FFmpeg capture options for this URL, in OpenCV's ``k;v|k;v`` form.

    RTSP gets `rtsp_transport;tcp` — the first line of the pre-submission
    checklist — plus a socket timeout so a half-open connection is noticed, and
    `reorder_queue_size;0`, which is meaningless over TCP but harmless and
    documents that we are not relying on UDP reordering.

    HTTP/HLS gets reconnect options instead: those are what let a byte-range or
    playlist reader survive a transient upstream blip without the whole session
    being torn down and rebuilt.
    """
    scheme = (urlparse(url).scheme or "").lower()
    if scheme in _RTSP_SCHEMES:
        return "|".join(
            [
                "rtsp_transport;tcp",
                f"timeout;{FFMPEG_TIMEOUT_US}",
                f"stimeout;{FFMPEG_TIMEOUT_US}",
                "reorder_queue_size;0",
                "max_delay;500000",
            ]
        )
    if scheme in {"http", "https"}:
        return "|".join(
            [
                "reconnect;1",
                "reconnect_streamed;1",
                "reconnect_delay_max;5",
                f"timeout;{FFMPEG_TIMEOUT_US}",
            ]
        )
    return ""


@dataclass(frozen=True)
class StreamCandidate:
    """One way to reach a camera's video, and what it is.

    `protocol` is descriptive only — nothing branches on it — but it is what
    gets reported when a camera turns out to be reachable one way and not
    another, which is the interesting operational fact.
    """

    url: str
    protocol: str = "rtsp"
    #: Set on a candidate the platform has to arrange (the relay pulling an
    #: awkward source) rather than one it can simply open.
    managed: bool = False


@dataclass
class Frame:
    """One decoded frame and its honest timing.

    `pts_s` is media time from the stream itself. `monotonic_s` is the clock
    every downstream stage should use: it is PTS where PTS works and a measured
    local clock where it does not, so nothing downstream needs to know which.
    """

    pixels: Any
    #: Presentation timestamp, seconds into the stream. None if unavailable.
    pts_s: float | None
    #: The timeline for sampling, tracking and dwell. Seconds, monotonic.
    monotonic_s: float
    #: Best estimate of the wall-clock instant this frame was captured at,
    #: as a POSIX timestamp. Anchored to PTS — see `PtsClock`.
    wall_ts: float
    #: True on the first frame after a loop point or a stream restart.
    discontinuity: bool = False
    #: True while media time is running ahead of real time, i.e. during the
    #: buffered-GOP replay at join. Nothing should infer motion or speed from
    #: these frames' *arrival*; their PTS deltas are still correct.
    catching_up: bool = False
    #: Index within the session, from zero.
    index: int = 0


@dataclass
class Backoff:
    """Exponential backoff with full jitter, and a floor of one attempt now.

    Full jitter rather than plain doubling because 80,000 cameras behind a
    supervisor that restarts a whole rack at once is a thundering herd, and the
    randomisation is what spreads the reconnects out.
    """

    base_s: float = BACKOFF_BASE_S
    cap_s: float = BACKOFF_CAP_S
    attempt: int = 0

    def next_delay(self) -> float:
        """Delay before the next attempt, and count this one."""
        window = min(self.cap_s, self.base_s * (2**self.attempt))
        self.attempt += 1
        # Never below a tenth of the window: full jitter can otherwise return
        # near-zero and produce exactly the tight loop the reference forbids.
        return random.uniform(window * 0.1, window)  # noqa: S311 - not cryptographic

    def reset(self) -> None:
        self.attempt = 0


@dataclass
class PtsClock:
    """Turns raw PTS readings into a timeline nothing downstream has to doubt.

    Three jobs.

    *Decide whether PTS is usable at all.* Some backends return 0.0 forever. If
    the reading has not advanced after a reasonable number of frames, this says
    so once and falls back to the local clock — reported, never silent, because
    "which clock is this camera on" changes how much its dwell times are worth.

    *Detect discontinuities.* PTS running backwards is the loop point; a large
    forward jump is a restart. Either way the frame is flagged and the timeline
    continues from where it was, so a tracker sees a cut rather than a leap.

    *Anchor media time to wall-clock time.* `wall = anchor + pts`, where the
    anchor is the smallest `wall_at_read - pts` seen this session. The minimum
    is the right estimator: the buffered replay at join makes early frames look
    *late* by however deep the buffer was, and every later observation of a
    genuinely live frame is closer to the truth, so the running minimum
    converges onto the least-delayed mapping and stays there.
    """

    #: Frames to give PTS before concluding it is not moving.
    probe_frames: int = 30

    pts_usable: bool | None = None
    last_pts: float | None = None
    #: Accumulated media time, continuous across discontinuities.
    elapsed_s: float = 0.0
    anchor: float | None = None
    frames: int = 0
    discontinuities: int = 0
    #: Local clock fallback, used only when PTS never advances.
    _fallback_started: float | None = None
    #: The session's first arrival, so that falling back to the local clock
    #: measures from the start of the session rather than from the moment we
    #: gave up on PTS — otherwise the timeline loses however many frames the
    #: probe took, and every dwell on that camera is short by that much.
    _first_mono: float | None = None
    #: Rolling window for the media-time-versus-wall-time rate, which is how
    #: the join burst is recognised. Two readings a second or so apart is
    #: enough; anything longer smooths the burst away.
    _rate_mark: tuple[float, float] | None = None
    _rate: float = 1.0

    def observe(self, raw_pts_ms: float | None, wall_now: float, mono_now: float) -> Frame:
        """Fold one raw PTS reading into the timeline. Returns a partial Frame.

        The caller fills in the pixels; this decides the time.
        """
        self.frames += 1
        if self._first_mono is None:
            self._first_mono = mono_now
        pts_s = None if raw_pts_ms is None or raw_pts_ms < 0 else raw_pts_ms / 1000.0

        if self.pts_usable is None:
            self._decide_usability(pts_s)

        # `pts_usable is None` means undecided, and undecided is treated as
        # *usable*. A stream that starts at PTS 0 — which is most of them —
        # would otherwise spend its first frames on the fallback clock and then
        # switch, putting a discontinuity into the timeline at the exact moment
        # a tracker is establishing itself.
        if self.pts_usable is False or pts_s is None:
            if self._fallback_started is None:
                self._fallback_started = self._first_mono
            self.elapsed_s = mono_now - self._fallback_started
            return Frame(
                pixels=None,
                pts_s=None,
                monotonic_s=self.elapsed_s,
                wall_ts=wall_now,
                index=self.frames - 1,
            )

        discontinuity = False
        if self.last_pts is not None:
            delta = pts_s - self.last_pts
            if delta < -PTS_REGRESSION_S or delta > PTS_JUMP_S:
                discontinuity = True
                self.discontinuities += 1
                # The timeline must stay monotonic across the cut, or every
                # duration downstream goes negative. Advance by a nominal step
                # rather than by the (meaningless) delta.
                delta = 0.0
                # The old anchor described the old segment; a loop restarts the
                # media clock and the mapping has to be re-derived.
                self.anchor = None
            self.elapsed_s += max(0.0, delta)
        self.last_pts = pts_s

        observed_anchor = wall_now - self.elapsed_s
        if self.anchor is None or observed_anchor < self.anchor:
            self.anchor = observed_anchor

        return Frame(
            pixels=None,
            pts_s=pts_s,
            monotonic_s=self.elapsed_s,
            wall_ts=self.anchor + self.elapsed_s,
            discontinuity=discontinuity,
            catching_up=self._advance_rate(wall_now) > CATCHUP_RATE,
            index=self.frames - 1,
        )

    def _advance_rate(self, wall_now: float) -> float:
        """Media seconds per wall second, over a short window.

        Comfortably above 1.0 means the gateway is still replaying its buffered
        group-of-pictures and frames are arriving faster than real time. That is
        the window in which a tracker timing by arrival invents impossible
        velocities, so downstream is told about it explicitly rather than being
        expected to infer it.
        """
        if self._rate_mark is None:
            self._rate_mark = (wall_now, self.elapsed_s)
            # Assume the burst at join rather than assuming live: the first
            # frames after connecting are exactly the ones being warned about.
            self._rate = 2.0
            return self._rate
        wall_then, media_then = self._rate_mark
        wall_delta = wall_now - wall_then
        if wall_delta >= RATE_WINDOW_S:
            self._rate = (self.elapsed_s - media_then) / wall_delta
            self._rate_mark = (wall_now, self.elapsed_s)
        return self._rate

    def _decide_usability(self, pts_s: float | None) -> None:
        """Settle whether this stream's PTS moves. Undecided until it must be.

        A reading of exactly 0.0 is ambiguous — it is both what a stream at its
        first frame reports and what a backend with no PTS support reports for
        ever — so the question is left open until the reading either advances
        (usable) or fails to advance across enough frames (not).
        """
        if pts_s is not None and pts_s > 0:
            self.pts_usable = True
            return
        if self.frames >= self.probe_frames:
            self.pts_usable = False
            log.warning(
                "stream reports no usable PTS after %d frames; falling back to the "
                "local clock — dwell and speed from this camera are weaker evidence",
                self.frames,
            )

    @property
    def source(self) -> str:
        """Which clock this session ended up on. Reported, not assumed."""
        if self.pts_usable is None:
            return "undetermined"
        return "pts" if self.pts_usable else "arrival"


@dataclass
class CaptureStats:
    """What a session actually did, for the metrics rollup.

    Measured, including the frame rate — because the reference is explicit that
    the declared one is not to be trusted, and a rate we did not measure is a
    rate we do not know.
    """

    frames: int = 0
    failed_reads: int = 0
    discontinuities: int = 0
    session_s: float = 0.0
    clock: str = "undetermined"
    endpoint: str | None = None
    protocol: str | None = None
    #: Declared rate, recorded only so it can be compared with the measured one.
    declared_fps: float | None = None
    width: int | None = None
    height: int | None = None

    @property
    def measured_fps(self) -> float | None:
        return self.frames / self.session_s if self.session_s > 0 else None


class LiveCapture:
    """Iterates frames from one camera, across reconnects, forever.

    Owns the endpoint ladder, the backoff, the join tolerance and the clock.
    The caller writes `for frame in capture.frames():` and gets frames with
    honest timestamps and a flag on the ones that follow a cut; every rule in
    the module docstring is discharged in here rather than at the call site.
    """

    def __init__(
        self,
        candidates: list[StreamCandidate],
        *,
        stop: threading.Event | None = None,
        label: str = "camera",
        buffer_frames: int = 2,
        on_stats: Any = None,
        on_exhausted: Any = None,
    ) -> None:
        if not candidates:
            raise ValueError("a capture needs at least one endpoint candidate")
        self.candidates = candidates
        self.stop = stop or threading.Event()
        self.label = label
        self.buffer_frames = buffer_frames
        self.on_stats = on_stats
        #: Called when no endpoint in the ladder opened. May return one more
        #: `StreamCandidate` to try — which is how the relay gets a chance at a
        #: source we cannot open ourselves, without this module knowing what a
        #: relay is.
        self.on_exhausted = on_exhausted
        self.backoff = Backoff()
        #: Index of the candidate that last worked. Tried first next time, so a
        #: camera that is only reachable over HLS does not pay for an RTSP
        #: timeout on every reconnect.
        self._preferred = 0
        self.stats = CaptureStats()

    # --- opening --------------------------------------------------------

    def _opened(self, url: str):  # noqa: ANN202
        """Open one URL with the right FFmpeg options. May return a dead handle."""
        import cv2

        options = ffmpeg_options_for(url)
        previous = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
        with _open_lock:
            if options:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = options
            try:
                capture = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            finally:
                if options:
                    if previous is None:
                        os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
                    else:
                        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = previous
        # A short buffer keeps us near the live edge. Analysing video from
        # thirty seconds ago is not live alerting.
        capture.set(cv2.CAP_PROP_BUFFERSIZE, self.buffer_frames)
        return capture

    def _ordered_candidates(self) -> list[tuple[int, StreamCandidate]]:
        order = list(range(len(self.candidates)))
        order.sort(key=lambda i: (i != self._preferred, i))
        return [(i, self.candidates[i]) for i in order]

    def _connect(self):  # noqa: ANN202
        """Walk the ladder until something opens. Returns (capture, candidate)."""
        for index, candidate in self._ordered_candidates():
            if self.stop.is_set():
                return None, None
            capture = self._opened(candidate.url)
            if capture.isOpened():
                if index != self._preferred:
                    log.info(
                        "%s: reachable over %s, not the preferred endpoint",
                        self.label, candidate.protocol,
                    )
                self._preferred = index
                return capture, candidate
            capture.release()
            log.debug("%s: %s endpoint did not open", self.label, candidate.protocol)
        return None, None

    def _extend(self) -> bool:
        """Ask the caller for one more endpoint. True if the ladder grew."""
        if self.on_exhausted is None:
            return False
        try:
            extra = self.on_exhausted()
        except Exception:  # noqa: BLE001 - a failed fallback is not fatal
            log.exception("%s: fallback endpoint lookup failed", self.label)
            return False
        if extra is None or any(c.url == extra.url for c in self.candidates):
            return False
        log.info("%s: adding a %s endpoint to the ladder", self.label, extra.protocol)
        self.candidates.append(extra)
        self._preferred = len(self.candidates) - 1
        return True

    # --- iteration ------------------------------------------------------

    def frames(self) -> Iterator[Frame]:
        """Yield frames until stopped. Reconnects on its own, with backoff."""
        first_session = True
        while not self.stop.is_set():
            capture, candidate = self._connect()
            if capture is None and self._extend():
                capture, candidate = self._connect()
            if capture is None:
                delay = self.backoff.next_delay()
                log.warning(
                    "%s: no endpoint opened (%d tried); retrying in %.1fs",
                    self.label, len(self.candidates), delay,
                )
                self.stop.wait(delay)
                continue

            # A reconnect is a discontinuity by definition: whatever the tracker
            # was following is long gone. The very first session is not — there
            # is no prior state to invalidate.
            yield from self._session(capture, candidate, cut=not first_session)
            first_session = False

            if self.stop.is_set():
                return
            delay = self.backoff.next_delay()
            log.info("%s: stream ended; reconnecting in %.1fs", self.label, delay)
            self.stop.wait(delay)

    def _session(self, capture: Any, candidate: StreamCandidate, *, cut: bool) -> Iterator[Frame]:
        import cv2

        clock = PtsClock()
        stats = CaptureStats(
            endpoint=candidate.url,
            protocol=candidate.protocol,
            # Read once, recorded, and used for nothing. See the module
            # docstring: the declared rate is evidence about the encoder's
            # metadata, not about the stream.
            declared_fps=_positive(capture.get(cv2.CAP_PROP_FPS)),
            width=_positive(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
            height=_positive(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        )
        self.stats = stats

        started = time.monotonic()
        last_frame_at = started
        consecutive_failures = 0
        pending_cut = cut

        log.info(
            "%s: decoding %s (declared %s fps, %sx%s — measured rate is what counts)",
            self.label, candidate.protocol,
            f"{stats.declared_fps:.1f}" if stats.declared_fps else "unknown",
            stats.width or "?", stats.height or "?",
        )

        try:
            while not self.stop.is_set():
                ok, pixels = capture.read()
                mono_now = time.monotonic()

                if not ok:
                    stats.failed_reads += 1
                    consecutive_failures += 1
                    joining = mono_now - started < JOIN_GRACE_S and stats.frames == 0
                    allowance = JOIN_GRACE_READS if joining else STEADY_GRACE_READS
                    if consecutive_failures <= allowance:
                        # Expected while the decoder waits for the first IDR, and
                        # survivable afterwards. Not an error, and above all not
                        # a reason to tear the session down.
                        if mono_now - last_frame_at > STALL_TIMEOUT_S:
                            log.warning("%s: no frame for %.0fs; reconnecting",
                                        self.label, mono_now - last_frame_at)
                            return
                        self.stop.wait(FAILED_READ_PAUSE_S)
                        continue
                    log.info(
                        "%s: %d consecutive failed reads%s; reconnecting",
                        self.label, consecutive_failures,
                        " during join" if joining else "",
                    )
                    return

                consecutive_failures = 0
                last_frame_at = mono_now
                stats.frames += 1

                timed = clock.observe(
                    _positive(capture.get(cv2.CAP_PROP_POS_MSEC)), time.time(), mono_now
                )
                timed.pixels = pixels
                if pending_cut:
                    timed.discontinuity = True
                    pending_cut = False
                if timed.discontinuity and stats.frames > 1:
                    log.info(
                        "%s: scene discontinuity at %.1fs (loop point or restart)",
                        self.label, timed.monotonic_s,
                    )

                stats.session_s = mono_now - started
                stats.discontinuities = clock.discontinuities
                stats.clock = clock.source

                # A session that survived this long is a working session, so the
                # next blip starts from a short delay rather than a long one.
                if stats.session_s >= SESSION_HEALTHY_S and self.backoff.attempt:
                    self.backoff.reset()

                yield timed
        finally:
            stats.session_s = time.monotonic() - started
            stats.clock = clock.source
            stats.discontinuities = clock.discontinuities
            capture.release()
            if self.on_stats is not None:
                self.on_stats(stats)
            log.info(
                "%s: session ended — %d frames in %.0fs (%.1f fps measured, declared %s), "
                "%d failed reads, %d discontinuities, clock=%s",
                self.label, stats.frames, stats.session_s,
                stats.measured_fps or 0.0,
                f"{stats.declared_fps:.1f}" if stats.declared_fps else "unknown",
                stats.failed_reads, stats.discontinuities, stats.clock,
            )


def _positive(value: Any) -> float | None:
    """A property reading, or None when the backend has nothing to say."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
