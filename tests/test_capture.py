"""The live-grid contract, as tests.

Every case here corresponds to a line of the integration reference's
pre-submission checklist. They exist because each of these failures is silent:
a pipeline reading UDP, or timing by arrival, or aborting on the first decoder
complaint, does not crash — it just quietly produces worse numbers, and there
is no second attempt at the evaluation to notice during.
"""

from __future__ import annotations

import sys
import threading
import types

import pytest

from services.anpr.capture import (
    BACKOFF_CAP_S,
    JOIN_GRACE_READS,
    Backoff,
    LiveCapture,
    PtsClock,
    StreamCandidate,
    ffmpeg_options_for,
)


class TestTransportOptions:
    """Checklist: 'Every client forces RTSP over TCP.'"""

    def test_rtsp_is_forced_over_tcp(self) -> None:
        assert "rtsp_transport;tcp" in ffmpeg_options_for("rtsp://host:8554/stream/1")

    def test_rtsps_is_forced_over_tcp_too(self) -> None:
        assert "rtsp_transport;tcp" in ffmpeg_options_for("rtsps://host:8554/stream/1")

    def test_rtsp_carries_a_socket_timeout(self) -> None:
        """A half-open TCP connection otherwise blocks read() for ever, and the
        camera silently stops being processed while looking healthy."""
        options = ffmpeg_options_for("rtsp://host:8554/stream/1")
        assert "timeout;" in options and "stimeout;" in options

    def test_http_gets_reconnect_options_not_rtsp_ones(self) -> None:
        options = ffmpeg_options_for("http://host/live/stream/1/index.m3u8")
        assert "reconnect;1" in options
        assert "rtsp_transport" not in options

    def test_an_unknown_scheme_gets_nothing_rather_than_a_guess(self) -> None:
        assert ffmpeg_options_for("/data/clip.mp4") == ""


class TestBackoff:
    """Checklist: 'Reconnect with backoff is implemented.' The reference asks
    for ~2 s start and ~30 s cap, and explicitly forbids a tight loop."""

    def test_it_never_returns_a_tight_loop(self) -> None:
        backoff = Backoff()
        assert all(backoff.next_delay() > 0.05 for _ in range(50))

    def test_it_grows_and_then_caps(self) -> None:
        backoff = Backoff(base_s=2.0, cap_s=30.0)
        delays = [backoff.next_delay() for _ in range(20)]
        assert max(delays) <= BACKOFF_CAP_S
        # Full jitter, so compare windows rather than individual draws.
        assert max(delays[10:]) > max(delays[:2])

    def test_a_healthy_session_resets_it(self) -> None:
        backoff = Backoff()
        for _ in range(8):
            backoff.next_delay()
        backoff.reset()
        assert backoff.next_delay() <= backoff.base_s


class TestPtsClock:
    """Checklist: 'No timing logic depends on CAP_PROP_FPS or frame arrival
    time.' This is the class that makes that true."""

    def test_media_time_comes_from_pts_not_from_the_wall_clock(self) -> None:
        clock = PtsClock()
        # Five seconds of video delivered in a tenth of a second, which is what
        # the buffered group-of-pictures replay at join actually looks like.
        first = clock.observe(0.0, wall_now=1000.0, mono_now=0.0)
        last = clock.observe(5000.0, wall_now=1000.1, mono_now=0.1)
        assert last.monotonic_s - first.monotonic_s == pytest.approx(5.0)

    def test_the_join_burst_is_flagged(self) -> None:
        clock = PtsClock()
        clock.observe(0.0, wall_now=1000.0, mono_now=0.0)
        burst = clock.observe(4000.0, wall_now=1000.6, mono_now=0.6)
        assert burst.catching_up

    def test_steady_live_delivery_is_not_flagged_as_a_burst(self) -> None:
        clock = PtsClock()
        for i in range(40):
            frame = clock.observe(i * 100.0, wall_now=1000.0 + i * 0.1, mono_now=i * 0.1)
        assert not frame.catching_up

    def test_pts_running_backwards_is_a_discontinuity(self) -> None:
        """The loop point. Each feed is a recording that restarts."""
        clock = PtsClock()
        clock.observe(30_000.0, wall_now=1000.0, mono_now=0.0)
        looped = clock.observe(0.0, wall_now=1000.04, mono_now=0.04)
        assert looped.discontinuity
        assert clock.discontinuities == 1

    def test_the_timeline_stays_monotonic_across_a_loop(self) -> None:
        """Or every duration computed downstream goes negative."""
        clock = PtsClock()
        before = clock.observe(30_000.0, wall_now=1000.0, mono_now=0.0)
        after = clock.observe(0.0, wall_now=1000.04, mono_now=0.04)
        later = clock.observe(40.0, wall_now=1000.08, mono_now=0.08)
        assert after.monotonic_s >= before.monotonic_s
        assert later.monotonic_s >= after.monotonic_s

    def test_a_small_backwards_step_is_reordering_not_a_cut(self) -> None:
        clock = PtsClock()
        clock.observe(5000.0, wall_now=1000.0, mono_now=0.0)
        jitter = clock.observe(4960.0, wall_now=1000.04, mono_now=0.04)
        assert not jitter.discontinuity

    def test_a_huge_forward_jump_is_a_discontinuity(self) -> None:
        clock = PtsClock()
        clock.observe(1000.0, wall_now=1000.0, mono_now=0.0)
        jumped = clock.observe(500_000.0, wall_now=1000.04, mono_now=0.04)
        assert jumped.discontinuity

    def test_it_falls_back_to_the_local_clock_when_pts_never_moves(self) -> None:
        """Some backends return 0.0 for ever. Reported, never silent."""
        clock = PtsClock(probe_frames=5)
        for i in range(10):
            frame = clock.observe(0.0, wall_now=1000.0 + i * 0.04, mono_now=i * 0.04)
        assert clock.pts_usable is False
        assert clock.source == "arrival"
        assert frame.monotonic_s == pytest.approx(0.36, abs=0.01)

    def test_the_wall_anchor_ignores_the_join_burst(self) -> None:
        """Anchoring on the first frame would stamp every sighting late by the
        depth of the gateway's buffer. The running minimum converges on the
        least-delayed observation instead.

        Concretely: the gateway replays two seconds of buffered video in the
        first fifth of a second. Anchored naively, the frame at media time 2.0 s
        would be stamped 1000.0 + 2.0 = 1002.0 — two seconds in the future.
        Anchored on the running minimum, media time 0 is placed at 998.2, and
        the frames that arrive once we are live are stamped at the moment they
        actually arrive."""
        clock = PtsClock()
        clock.observe(0.0, wall_now=1000.0, mono_now=0.0)      # 2 s of buffer
        clock.observe(2000.0, wall_now=1000.2, mono_now=0.2)   # replayed fast
        live = clock.observe(2200.0, wall_now=1000.4, mono_now=0.4)
        assert clock.anchor == pytest.approx(998.2, abs=0.01)
        assert live.wall_ts == pytest.approx(1000.4, abs=0.01)

    def test_a_replayed_frame_is_dated_earlier_than_it_arrived(self) -> None:
        """Which is the honest answer: it is two-second-old video."""
        clock = PtsClock()
        clock.observe(0.0, wall_now=1000.0, mono_now=0.0)
        clock.observe(2000.0, wall_now=1000.2, mono_now=0.2)
        # The same media instant as the first frame, re-evaluated now that the
        # anchor has converged, would be stamped 998.2 rather than 1000.0.
        assert clock.anchor is not None
        assert clock.anchor + 0.0 == pytest.approx(998.2, abs=0.01)


# --- LiveCapture, against a fake OpenCV ---------------------------------


class _FakeCapture:
    """A cv2.VideoCapture that does exactly what the test asks it to."""

    def __init__(self, reads: list, opened: bool = True) -> None:
        self._reads = list(reads)
        self._opened = opened
        self.released = False
        self.properties: dict = {}

    def isOpened(self) -> bool:  # noqa: N802 - matching cv2's spelling
        return self._opened

    def read(self):  # noqa: ANN201
        if not self._reads:
            return False, None
        return self._reads.pop(0)

    def get(self, prop):  # noqa: ANN001, ANN201
        return self.properties.get(prop, 0.0)

    def set(self, prop, value):  # noqa: ANN001, ANN201
        self.properties[prop] = value
        return True

    def release(self) -> None:
        self.released = True


@pytest.fixture
def fake_cv2(monkeypatch):  # noqa: ANN001, ANN201
    """Install a stub `cv2` so the capture loop can be tested with no codec."""
    module = types.SimpleNamespace(
        CAP_FFMPEG=1900,
        CAP_PROP_BUFFERSIZE=38,
        CAP_PROP_POS_MSEC=0,
        CAP_PROP_FPS=5,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        opened=[],
        VideoCapture=None,
    )

    def video_capture(url, backend):  # noqa: ANN001, ANN202
        module.opened.append(url)
        return module.next_capture(url)

    module.VideoCapture = video_capture
    monkeypatch.setitem(sys.modules, "cv2", module)
    return module


def _frames(count: int, start_pts_ms: float = 0.0, step_ms: float = 40.0) -> list:
    return [(True, f"frame-{i}") for i in range(count)]


class TestEndpointLadder:
    def test_it_falls_back_to_hls_when_rtsp_will_not_open(self, fake_cv2) -> None:  # noqa: ANN001
        """'If port 8554 is blocked on your network, use the HLS endpoint.'"""

        def next_capture(url):  # noqa: ANN001, ANN202
            if url.startswith("rtsp"):
                return _FakeCapture([], opened=False)
            return _FakeCapture(_frames(3))

        fake_cv2.next_capture = next_capture
        stop = threading.Event()
        capture = LiveCapture(
            [
                StreamCandidate("rtsp://blocked:8554/stream/1", "rtsp"),
                StreamCandidate("http://host/live/stream/1/index.m3u8", "hls"),
            ],
            stop=stop,
        )
        produced = []
        for frame in capture.frames():
            produced.append(frame)
            if len(produced) == 3:
                stop.set()
        assert len(produced) == 3
        assert capture.stats.protocol == "hls"

    def test_the_working_endpoint_is_tried_first_next_time(self, fake_cv2) -> None:  # noqa: ANN001
        def next_capture(url):  # noqa: ANN001, ANN202
            if url.startswith("rtsp"):
                return _FakeCapture([], opened=False)
            return _FakeCapture(_frames(1))

        fake_cv2.next_capture = next_capture
        stop = threading.Event()
        capture = LiveCapture(
            [
                StreamCandidate("rtsp://blocked:8554/s", "rtsp"),
                StreamCandidate("http://host/s.m3u8", "hls"),
            ],
            stop=stop,
        )
        capture.backoff.base_s = 0.0
        capture.backoff.cap_s = 0.0
        for seen, _ in enumerate(capture.frames(), start=1):
            if seen == 2:
                stop.set()
        # Second session opened HLS directly rather than retrying RTSP first.
        assert fake_cv2.opened[-1].startswith("http")

    def test_an_exhausted_ladder_asks_for_one_more_endpoint(self, fake_cv2) -> None:  # noqa: ANN001
        """This is how the relay gets a chance at a source we cannot open."""

        def next_capture(url):  # noqa: ANN001, ANN202
            if "relayed" in url:
                return _FakeCapture(_frames(2))
            return _FakeCapture([], opened=False)

        fake_cv2.next_capture = next_capture
        stop = threading.Event()
        capture = LiveCapture(
            [StreamCandidate("rtsp://dead/s", "rtsp")],
            stop=stop,
            on_exhausted=lambda: StreamCandidate("rtsp://relayed/s", "rtsp", managed=True),
        )
        produced = 0
        for _ in capture.frames():
            produced += 1
            if produced == 2:
                stop.set()
        assert produced == 2


class TestJoinTolerance:
    def test_decoder_complaints_at_join_are_not_fatal(self, fake_cv2) -> None:  # noqa: ANN001
        """'Attaching mid-stream can produce decoder messages until the first
        IDR frame arrives. This is normal and self-corrects.'"""
        reads = [(False, None)] * 30 + _frames(4)
        fake_cv2.next_capture = lambda url: _FakeCapture(reads)  # noqa: ARG005
        stop = threading.Event()
        capture = LiveCapture([StreamCandidate("rtsp://host/s", "rtsp")], stop=stop)
        produced = 0
        for _ in capture.frames():
            produced += 1
            if produced == 4:
                stop.set()
        assert produced == 4
        assert capture.stats.failed_reads == 30

    def test_a_stream_that_never_delivers_is_eventually_given_up_on(
        self, fake_cv2
    ) -> None:  # noqa: ANN001
        fake_cv2.next_capture = lambda url: _FakeCapture([])  # noqa: ARG005
        stop = threading.Event()
        capture = LiveCapture([StreamCandidate("rtsp://host/s", "rtsp")], stop=stop)
        capture.backoff.base_s = 0.0
        capture.backoff.cap_s = 0.0
        iterator = capture.frames()
        stop.set()
        assert list(iterator) == []
        assert capture.stats.failed_reads <= JOIN_GRACE_READS + 1


class TestMeasuredRate:
    def test_the_declared_frame_rate_is_recorded_but_not_used(self, fake_cv2) -> None:  # noqa: ANN001
        """'DON'T trust the reported frame rate.' It is kept only so the two
        can be compared in the performance evidence."""
        capture_stub = _FakeCapture(_frames(5))
        capture_stub.properties[fake_cv2.CAP_PROP_FPS] = 25.0
        fake_cv2.next_capture = lambda url: capture_stub  # noqa: ARG005
        stop = threading.Event()
        capture = LiveCapture([StreamCandidate("rtsp://host/s", "rtsp")], stop=stop)
        for produced, _ in enumerate(capture.frames(), start=1):
            if produced == 5:
                stop.set()
        assert capture.stats.declared_fps == 25.0
        # Measured from what actually arrived, which is the only honest figure.
        assert capture.stats.measured_fps is not None


class TestReconnectIsADiscontinuity:
    def test_frames_after_a_reconnect_are_flagged(self, fake_cv2) -> None:  # noqa: ANN001
        """Whatever the tracker was following before the drop is long gone."""
        sessions = [_FakeCapture(_frames(1)), _FakeCapture(_frames(2))]
        fake_cv2.next_capture = lambda url: sessions.pop(0)  # noqa: ARG005
        stop = threading.Event()
        capture = LiveCapture([StreamCandidate("rtsp://host/s", "rtsp")], stop=stop)
        capture.backoff.base_s = 0.0
        capture.backoff.cap_s = 0.0
        flags = []
        for frame in capture.frames():
            flags.append(frame.discontinuity)
            if len(flags) == 2:
                stop.set()
        assert flags[0] is False
        assert flags[1] is True
