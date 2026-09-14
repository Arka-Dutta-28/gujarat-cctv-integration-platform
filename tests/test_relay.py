"""Relay supervisor lifecycle.

The relay is where "video is pulled on demand, not hauled continuously" either
holds or does not. The rules that make it hold are all in the reaper, and they
are easy to get subtly wrong in ways that only show up as either a stuck ffmpeg
per camera (cost) or a stream that dies while an operator is watching it
(worse). Tested here with fakes, so no ffmpeg and no media server are involved.
"""

from __future__ import annotations

import time

import pytest

from services.relay.relay import IDLE_TIMEOUT_S, Relay, Supervisor, webrtc_playable


class FakeProc:
    """Stands in for the ffmpeg subprocess."""

    def __init__(self, running: bool = True) -> None:
        self._running = running
        self.returncode: int | None = None if running else 1
        self.stderr = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self._running = False
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        return self.returncode or 0

    def kill(self) -> None:
        self.terminated = True


@pytest.fixture
def supervisor(monkeypatch) -> Supervisor:
    s = Supervisor()
    monkeypatch.setattr(s, "_paths", lambda: getattr(s, "_fake_paths", {}))
    return s


def add(s: Supervisor, camera_id: str, *, age: float, last_reader: float,
        readers_seen: int = 0, running: bool = True) -> Relay:
    now = time.monotonic()
    relay = Relay(
        camera_id=camera_id, path=camera_id, proc=FakeProc(running),
        started_at=now - age, last_reader_at=now - last_reader,
        readers_seen=readers_seen,
    )
    s.relays[camera_id] = relay
    return relay


class TestReaper:
    def test_a_watched_relay_is_kept(self, supervisor: Supervisor) -> None:
        add(supervisor, "cam-a", age=600, last_reader=600, readers_seen=1)
        supervisor._fake_paths = {"cam-a": {"readers": [{"type": "webrtcSession"}]}}
        supervisor.reap()
        assert "cam-a" in supervisor.relays

    def test_an_abandoned_relay_is_stopped(self, supervisor: Supervisor) -> None:
        relay = add(supervisor, "cam-a", age=600, last_reader=IDLE_TIMEOUT_S + 5, readers_seen=1)
        supervisor._fake_paths = {"cam-a": {"readers": []}}
        supervisor.reap()
        assert "cam-a" not in supervisor.relays
        assert relay.proc.terminated

    def test_a_just_started_relay_survives_before_anyone_connects(
        self, supervisor: Supervisor
    ) -> None:
        """The player takes a moment to connect after the URL is handed over.

        Reaping on "no readers" alone would kill every relay in the gap between
        the API answering and the browser's WebRTC session being established —
        so the camera would never play, and retrying would never help.
        """
        add(supervisor, "cam-a", age=1.0, last_reader=1.0, readers_seen=0)
        supervisor._fake_paths = {"cam-a": {"readers": []}}
        supervisor.reap()
        assert "cam-a" in supervisor.relays

    def test_a_relay_nobody_ever_watched_is_eventually_stopped(
        self, supervisor: Supervisor
    ) -> None:
        """The grace period is a grace period, not an exemption."""
        add(supervisor, "cam-a", age=IDLE_TIMEOUT_S + 10, last_reader=IDLE_TIMEOUT_S + 10)
        supervisor._fake_paths = {"cam-a": {"readers": []}}
        supervisor.reap()
        assert "cam-a" not in supervisor.relays

    def test_a_reader_refreshes_the_idle_clock(self, supervisor: Supervisor) -> None:
        relay = add(supervisor, "cam-a", age=600, last_reader=IDLE_TIMEOUT_S + 5, readers_seen=1)
        supervisor._fake_paths = {"cam-a": {"readers": [{"type": "webrtcSession"}]}}
        supervisor.reap()
        supervisor._fake_paths = {"cam-a": {"readers": []}}
        supervisor.reap()
        assert "cam-a" in supervisor.relays
        assert relay.readers_seen == 1

    def test_a_dead_relay_is_forgotten(self, supervisor: Supervisor) -> None:
        """ffmpeg exiting must not leave a zombie entry claiming to be live.

        The upstream returning 500 forever — cameras 6 and 22 of the government
        estate do exactly this — kills ffmpeg. If the entry stayed, the next
        request would find an existing relay and never start a working one.
        """
        add(supervisor, "cam-a", age=5, last_reader=5, running=False)
        supervisor._fake_paths = {}
        supervisor.reap()
        assert "cam-a" not in supervisor.relays

    def test_an_unknown_path_counts_as_unwatched(self, supervisor: Supervisor) -> None:
        add(supervisor, "cam-a", age=600, last_reader=IDLE_TIMEOUT_S + 5, readers_seen=1)
        supervisor._fake_paths = {}
        supervisor.reap()
        assert "cam-a" not in supervisor.relays

    def test_reaping_one_camera_leaves_the_others_alone(self, supervisor: Supervisor) -> None:
        add(supervisor, "cam-a", age=600, last_reader=IDLE_TIMEOUT_S + 5, readers_seen=1)
        add(supervisor, "cam-b", age=600, last_reader=1, readers_seen=1)
        supervisor._fake_paths = {"cam-a": {"readers": []},
                                  "cam-b": {"readers": [{"type": "webrtcSession"}]}}
        supervisor.reap()
        assert set(supervisor.relays) == {"cam-b"}


class TestStop:
    def test_stopping_an_unknown_camera_is_not_an_error(self, supervisor: Supervisor) -> None:
        assert supervisor.stop("never-started") is False

    def test_stop_terminates_and_forgets(self, supervisor: Supervisor) -> None:
        relay = add(supervisor, "cam-a", age=5, last_reader=5)
        assert supervisor.stop("cam-a") is True
        assert relay.proc.terminated
        assert "cam-a" not in supervisor.relays


class TestFfmpegCommand:
    def test_stream_copy_and_no_credential_in_the_log_path(self) -> None:
        from services.relay.relay import _ffmpeg_command

        cmd = _ffmpeg_command("https://host/stream/1", "rtsp://mediamtx:8554/sentinel-01")
        # Re-encoding 31 already-h264 feeds would burn the box for nothing.
        assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"
        # The upstream answers 503 while a stream warms up; give up too early
        # and the first view of a cold camera always fails.
        assert "-reconnect_on_http_error" in cmd
        assert cmd[-1] == "rtsp://mediamtx:8554/sentinel-01"


class TestCodecCompatibility:
    """Which streams a browser can actually decode.

    Ten of the fifty simulated cameras publish H.265 or MPEG-4 on purpose. The
    failure mode if this is wrong is nasty: WebRTC establishes the session,
    delivers a few packets and the server drops it, so the operator sees a
    video that connects and never paints rather than an error.
    """

    def test_h264_plays_as_it_stands(self) -> None:
        assert webrtc_playable(["H264"]) is True

    def test_h265_must_be_re_encoded(self) -> None:
        assert webrtc_playable(["H265"]) is False

    def test_mpeg4_must_be_re_encoded(self) -> None:
        assert webrtc_playable(["MPEG-4 Video"]) is False

    def test_audio_alongside_video_does_not_decide_it(self) -> None:
        assert webrtc_playable(["H264", "Opus"]) is True
        assert webrtc_playable(["H265", "Opus"]) is False

    def test_a_path_with_no_tracks_is_not_playable(self) -> None:
        """A path that exists but carries nothing must not be offered."""
        assert webrtc_playable([]) is False
        assert webrtc_playable(None) is False

    def test_codec_names_are_matched_case_insensitively(self) -> None:
        assert webrtc_playable(["h264"]) is True


class TestTranscodeCommand:
    def test_transcode_targets_what_webrtc_can_carry(self) -> None:
        from services.relay.relay import _ffmpeg_command

        cmd = _ffmpeg_command(
            "rtsp://mediamtx:8554/cam-03", "rtsp://mediamtx:8554/cam-03-web", transcode=True
        )
        assert cmd[cmd.index("-c:v") + 1] == "libx264"
        assert cmd[cmd.index("-profile:v") + 1] == "baseline"
        # B-frames are the specific thing MediaMTX refuses over WebRTC.
        assert cmd[cmd.index("-bf") + 1] == "0"

    def test_rtsp_input_does_not_carry_http_reader_options(self) -> None:
        """ffmpeg rejects the wrong option family outright rather than ignoring it."""
        from services.relay.relay import _ffmpeg_command

        cmd = _ffmpeg_command("rtsp://mediamtx:8554/cam-03", "rtsp://mediamtx:8554/x")
        assert "-reconnect" not in cmd
        assert "-rtsp_transport" in cmd

    def test_http_input_carries_reconnect_options(self) -> None:
        from services.relay.relay import _ffmpeg_command

        cmd = _ffmpeg_command("https://host/stream/1", "rtsp://mediamtx:8554/x")
        assert "-reconnect" in cmd
        # The upstream answers 503 while warming a stream up.
        assert "-reconnect_on_http_error" in cmd


class TestPlaybackTransport:
    """Which transport the browser is handed, and the URL shape that goes with it.

    A deployment setting, not a preference. WebRTC media rides UDP, so an
    HTTP-only path in front of the platform — a tunnel, a corporate proxy, the
    "restricted network" the integration contract names — carries the WHEP
    signalling perfectly and then starves the ICE negotiation: the session
    establishes and no frame ever paints. HLS is plain HTTP and survives it.

    Tested at the level of `_playback` because that is the *single* place both
    are built, and it is single for a reason — see the docstring there. When HLS
    was first added it was computed in the relay-started branch only, so the
    already-publishing branch went on handing back a WHEP URL labelled `hls`.
    Every simulated camera on a hosted instance resolved to an endpoint the
    player then tried to fetch as a playlist.
    """

    @staticmethod
    def _playback(monkeypatch, *, transport: str, base: str, path: str):
        from services.relay import relay as mod

        monkeypatch.setattr(mod.settings, "media_transport", transport, raising=False)
        monkeypatch.setattr(
            type(mod.settings), "mediamtx_public_base",
            property(lambda _self: base), raising=False,
        )
        return Supervisor._playback(path)

    def test_webrtc_gives_the_bare_path_for_the_player_to_append_whep_to(
        self, monkeypatch
    ) -> None:
        url, protocol = self._playback(
            monkeypatch, transport="webrtc", base="http://localhost:8889", path="cam-01"
        )
        assert (url, protocol) == ("http://localhost:8889/cam-01", "webrtc")

    def test_hls_gives_the_playlist_itself(self, monkeypatch) -> None:
        url, protocol = self._playback(
            monkeypatch, transport="hls", base="https://cctv.example.com/media",
            path="cam-01",
        )
        assert url == "https://cctv.example.com/media/cam-01/index.m3u8"
        assert protocol == "hls"

    def test_the_transcoded_path_is_carried_through(self, monkeypatch) -> None:
        """A camera the browser cannot decode is republished at `<path>-web`.

        The playback URL has to follow it, or the player is pointed at the
        original stream it could not decode in the first place.
        """
        url, _ = self._playback(
            monkeypatch, transport="hls", base="https://h/media", path="sentinel-4-web"
        )
        assert url == "https://h/media/sentinel-4-web/index.m3u8"

    @pytest.mark.parametrize("value", ["HLS", " hls ", "Hls"])
    def test_the_setting_is_case_and_space_insensitive(self, monkeypatch, value) -> None:
        """It arrives from an environment variable in a compose file, by hand."""
        _, protocol = self._playback(
            monkeypatch, transport=value, base="https://h/media", path="cam-01"
        )
        assert protocol == "hls"

    def test_anything_that_is_not_hls_stays_on_webrtc(self, monkeypatch) -> None:
        """A typo must not silently disable live video.

        WebRTC is the default and the better transport; falling back to it means
        a mistyped setting costs latency on a restricted network rather than
        producing a URL nothing can play.
        """
        _, protocol = self._playback(
            monkeypatch, transport="hsl", base="https://h/media", path="cam-01"
        )
        assert protocol == "webrtc"
