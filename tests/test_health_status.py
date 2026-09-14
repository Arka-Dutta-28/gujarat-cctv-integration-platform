"""Health rules. The two field-observation lessons are pinned as tests."""

from __future__ import annotations

import pytest

from services.health.status import (
    CLOCK_SKEW_WARN_S,
    STALL_SECONDS,
    CameraStatus,
    StreamSample,
    assess,
)


class TestOnline:
    def test_streaming_camera_is_online(self) -> None:
        v = assess(StreamSample(ready=True, seconds_since_data=0.5))
        assert v.status is CameraStatus.ONLINE

    def test_camera_seeing_no_traffic_is_still_online(self) -> None:
        """A market stall and an empty lane are healthy cameras.

        docs/field-observations.md §5: several real feeds will never produce a
        plate read. Treating zero detections as a fault would show them red.
        """
        v = assess(
            StreamSample(ready=True, seconds_since_data=1.0, bitrate_bps=800_000,
                         expected_bitrate_bps=1_000_000)
        )
        assert v.status is CameraStatus.ONLINE

    def test_bitrate_above_threshold_stays_online(self) -> None:
        v = assess(
            StreamSample(ready=True, seconds_since_data=1.0,
                         bitrate_bps=500_000, expected_bitrate_bps=1_000_000)
        )
        assert v.status is CameraStatus.ONLINE


class TestOffline:
    def test_no_publisher_is_offline(self) -> None:
        v = assess(StreamSample(ready=False))
        assert v.status is CameraStatus.OFFLINE
        assert "no publisher" in v.reason


class TestDegraded:
    def test_stalled_publisher_is_degraded_not_offline(self) -> None:
        v = assess(StreamSample(ready=True, seconds_since_data=STALL_SECONDS + 5))
        assert v.status is CameraStatus.DEGRADED
        assert "no data" in v.reason

    def test_just_under_stall_threshold_is_online(self) -> None:
        v = assess(StreamSample(ready=True, seconds_since_data=STALL_SECONDS - 0.1))
        assert v.status is CameraStatus.ONLINE

    def test_collapsed_bitrate_is_degraded(self) -> None:
        v = assess(
            StreamSample(ready=True, seconds_since_data=1.0,
                         bitrate_bps=50_000, expected_bitrate_bps=1_000_000)
        )
        assert v.status is CameraStatus.DEGRADED
        assert "below" in v.reason

    def test_bitrate_ignored_when_no_expectation_recorded(self) -> None:
        v = assess(StreamSample(ready=True, seconds_since_data=1.0, bitrate_bps=1))
        assert v.status is CameraStatus.ONLINE


class TestProbeFailure:
    def test_probe_failure_is_unknown_not_offline(self) -> None:
        """A failure to look says nothing about the camera.

        Reporting offline here would turn the whole map red the moment the
        media server hiccups, which is worse than admitting we do not know.
        """
        v = assess(StreamSample(ready=False, probe_failed=True, error="connection refused"))
        assert v.status is CameraStatus.UNKNOWN
        assert "connection refused" in v.reason


class TestClockSkew:
    """docs/field-observations.md §3 — real camera clocks are wrong by weeks."""

    def test_large_skew_is_flagged_but_camera_stays_online(self) -> None:
        v = assess(
            StreamSample(ready=True, seconds_since_data=1.0,
                         clock_skew_s=-5_000_000)  # ~2 months slow, as observed
        )
        assert v.status is CameraStatus.ONLINE, "a wrong clock is not a dead camera"
        assert v.clock_suspect is True
        assert "clock" in v.reason

    def test_small_skew_is_not_flagged(self) -> None:
        v = assess(
            StreamSample(ready=True, seconds_since_data=1.0,
                         clock_skew_s=CLOCK_SKEW_WARN_S - 1)
        )
        assert v.clock_suspect is False

    def test_skew_never_makes_a_camera_offline(self) -> None:
        for skew in (-10_000_000, -3600, 0, 3600, 10_000_000):
            v = assess(StreamSample(ready=True, seconds_since_data=1.0, clock_skew_s=skew))
            assert v.status is CameraStatus.ONLINE

    def test_unknown_skew_is_not_suspect(self) -> None:
        v = assess(StreamSample(ready=True, seconds_since_data=1.0, clock_skew_s=None))
        assert v.clock_suspect is False


@pytest.mark.parametrize(
    "sample",
    [
        StreamSample(ready=True),
        StreamSample(ready=False),
        StreamSample(ready=True, seconds_since_data=999),
        StreamSample(ready=False, probe_failed=True),
    ],
)
def test_every_verdict_carries_a_reason(sample: StreamSample) -> None:
    """The operator sees the reason, not the enum; it must never be empty."""
    assert assess(sample).reason


class TestBitrateTracking:
    """The learned-baseline logic in the prober's rolling state."""

    def test_bitrate_computed_from_byte_delta(self) -> None:
        from services.health.prober import _Track

        t = _Track()
        assert t.observe_bytes(0, 100.0) == (None, 0.0)  # first sample, no rate yet
        bitrate, since = t.observe_bytes(125_000, 101.0)
        assert bitrate == pytest.approx(1_000_000)  # 125 kB in 1 s = 1 Mbit/s
        assert since == 0.0

    def test_seconds_since_data_grows_while_counter_is_frozen(self) -> None:
        from services.health.prober import _Track

        t = _Track()
        t.observe_bytes(1000, 100.0)
        t.observe_bytes(1000, 105.0)
        _, since = t.observe_bytes(1000, 110.0)
        assert since == pytest.approx(10.0)

    def test_no_expectation_until_enough_samples(self) -> None:
        """Prevents a cold prober from calling every camera degraded."""
        from services.health.prober import MIN_SAMPLES_BEFORE_BITRATE_JUDGEMENT, _Track

        t = _Track()
        for i in range(MIN_SAMPLES_BEFORE_BITRATE_JUDGEMENT):
            assert t.expectation(1_000_000) is None, f"sample {i} should not judge yet"
        assert t.expectation(1_000_000) == pytest.approx(1_000_000, rel=0.01)

    def test_counter_reset_does_not_produce_negative_bitrate(self) -> None:
        """A republished stream restarts its byte counter."""
        from services.health.prober import _Track

        t = _Track()
        t.observe_bytes(1_000_000, 100.0)
        bitrate, _ = t.observe_bytes(0, 101.0)
        assert bitrate is None or bitrate >= 0


class TestDebounce:
    """Degradations are confirmed over several cycles; recovery is immediate.

    The farm's 4 fps degraded camera swings 3-4x in bitrate between samples, so
    without this it flaps online/degraded and corrupts the uptime figures.
    """

    def _prober(self):
        from services.health.prober import Prober

        return Prober("postgresql://unused", "http://unused", 5.0)

    def _cam(self, status="online"):
        from services.health.prober import CameraRow

        return CameraRow(
            id="c1", external_ref="cam-15", name="n",
            stream_ref="rtsp://mediamtx:8554/cam-15", adapter="rtsp", status=status,
        )

    def test_single_dip_does_not_change_status(self) -> None:
        from services.health.status import CameraStatus, HealthVerdict

        p, cam = self._prober(), self._cam("online")
        out = p._debounce(cam, HealthVerdict(CameraStatus.DEGRADED, "bitrate dip"))
        assert out.status is CameraStatus.ONLINE
        assert "unconfirmed" in out.reason

    def test_repeated_degradation_is_committed(self) -> None:
        from services.health.prober import DEGRADE_CONFIRMATIONS
        from services.health.status import CameraStatus, HealthVerdict

        p, cam = self._prober(), self._cam("online")
        for _ in range(DEGRADE_CONFIRMATIONS - 1):
            assert p._debounce(
                cam, HealthVerdict(CameraStatus.DEGRADED, "dip")
            ).status is CameraStatus.ONLINE
        assert p._debounce(
            cam, HealthVerdict(CameraStatus.DEGRADED, "dip")
        ).status is CameraStatus.DEGRADED

    def test_recovery_is_immediate(self) -> None:
        from services.health.status import CameraStatus, HealthVerdict

        p, cam = self._prober(), self._cam("offline")
        out = p._debounce(cam, HealthVerdict(CameraStatus.ONLINE, "streaming"))
        assert out.status is CameraStatus.ONLINE, "a recovered camera must show green at once"

    def test_new_camera_turns_green_on_first_probe(self) -> None:
        """M1 acceptance: onboard a camera and it turns green in under 30 s."""
        from services.health.status import CameraStatus, HealthVerdict

        p, cam = self._prober(), self._cam("unknown")
        out = p._debounce(cam, HealthVerdict(CameraStatus.ONLINE, "streaming"))
        assert out.status is CameraStatus.ONLINE

    def test_alternating_verdicts_never_confirm(self) -> None:
        from services.health.status import CameraStatus, HealthVerdict

        p, cam = self._prober(), self._cam("online")
        for _ in range(10):
            p._debounce(cam, HealthVerdict(CameraStatus.DEGRADED, "dip"))
            out = p._debounce(cam, HealthVerdict(CameraStatus.ONLINE, "streaming"))
        assert out.status is CameraStatus.ONLINE


# --- probe_http content-type gate ------------------------------------------
#
# Regression cover for 1 Sep 2026: the grid's old hostname began 301-ing to a
# login page. urllib followed the redirect, the page answered 200, and the
# prober reported thirty cameras as "streaming" while every one served a form.


class _FakeResponse:
    def __init__(self, status: int, content_type: str) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self, _n: int = 1) -> bytes:
        return b"\x00"

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _probe_returning(monkeypatch, status: int, content_type: str):
    from services.health import prober

    monkeypatch.setattr(
        prober.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(status, content_type),
    )
    return prober.probe_http("https://example.invalid/stream/1")


def test_probe_http_rejects_an_html_login_page(monkeypatch):
    sample = _probe_returning(monkeypatch, 200, "text/html; charset=utf-8")
    assert sample.ready is False
    assert "not media" in (sample.error or "")


def test_probe_http_accepts_video(monkeypatch):
    sample = _probe_returning(monkeypatch, 200, "video/mp2t")
    assert sample.ready is True
    assert sample.error is None


def test_probe_http_accepts_a_playlist_served_as_text_plain(monkeypatch):
    # Some servers mislabel .m3u8 as text/plain. Rejecting it would black out
    # working cameras, so the gate is HTML-only by design.
    sample = _probe_returning(monkeypatch, 200, "text/plain")
    assert sample.ready is True


def test_probe_http_tolerates_a_missing_content_type(monkeypatch):
    sample = _probe_returning(monkeypatch, 200, "")
    assert sample.ready is True
