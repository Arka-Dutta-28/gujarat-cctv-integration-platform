"""Adapter spine.

The load-bearing test here is `test_adding_an_adapter_touches_nothing_else`:
M2's acceptance says a new adapter type must require no changes outside its own
module, and that is only true if nothing else enumerates adapter names.
"""

from __future__ import annotations

import pytest

from services.adapters import CameraRef, available_adapters, get_adapter, register_adapter
from services.adapters.base import CameraAdapter, PlaybackTarget
from services.adapters.credentials import Credential, apply_to_url, redact, resolve


def ref(
    adapter: str, stream: str, cred: str | None = None, ext: str | None = "cam-01"
) -> CameraRef:
    return CameraRef(
        id="0d1c1e00-0000-0000-0000-000000000001",
        external_ref=ext, name="Test", adapter=adapter,
        stream_ref=stream, credential_ref=cred,
    )


class TestRegistry:
    def test_builtin_adapters_are_registered(self) -> None:
        assert set(available_adapters()) >= {"rtsp", "http", "hls", "file", "onvif"}

    def test_every_registry_adapter_value_resolves(self) -> None:
        """Any `adapter` the database permits must have an implementation."""
        for name in ("rtsp", "http", "hls", "file", "onvif"):
            assert get_adapter(name).name == name

    def test_unknown_adapter_raises_with_a_useful_message(self) -> None:
        with pytest.raises(LookupError, match="no adapter for 'smoke-signal'"):
            get_adapter("smoke-signal")

    def test_adding_an_adapter_touches_nothing_else(self) -> None:
        """M2 acceptance: a new adapter is one self-contained module."""

        @register_adapter
        class CarrierPigeonAdapter(CameraAdapter):
            name = "carrier_pigeon"
            needs_relay = True

            def playback(self, camera: CameraRef) -> PlaybackTarget:
                return PlaybackTarget(camera_id=camera.id, protocol="webrtc", url="x")

            def probe_url(self, camera: CameraRef) -> str:
                return camera.stream_ref

        try:
            assert get_adapter("carrier_pigeon").name == "carrier_pigeon"
            assert "carrier_pigeon" in available_adapters()
        finally:
            from services.adapters.base import _ADAPTERS

            _ADAPTERS.pop("carrier_pigeon", None)

    def test_duplicate_registration_is_refused(self) -> None:
        with pytest.raises(ValueError, match="already registered"):
            @register_adapter
            class Dupe(CameraAdapter):
                name = "rtsp"

                def playback(self, camera: CameraRef) -> PlaybackTarget:
                    raise NotImplementedError

                def probe_url(self, camera: CameraRef) -> str:
                    raise NotImplementedError

    def test_adapter_without_a_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must set a `name`"):
            @register_adapter
            class Nameless(CameraAdapter):
                def playback(self, camera: CameraRef) -> PlaybackTarget:
                    raise NotImplementedError

                def probe_url(self, camera: CameraRef) -> str:
                    raise NotImplementedError


class TestPlayback:
    def test_rtsp_is_relayed_to_webrtc(self) -> None:
        """Browsers cannot play RTSP."""
        t = get_adapter("rtsp").playback(ref("rtsp", "rtsp://mediamtx:8554/cam-01"))
        assert t.protocol == "webrtc"
        assert t.requires_relay is True

    def test_progressive_http_is_relayed(self) -> None:
        """The government feeds are progressive, not live-seekable streams."""
        t = get_adapter("http").playback(ref("http", "https://live.example.in/stream/1"))
        assert t.protocol == "webrtc"
        assert t.requires_relay is True

    def test_unauthenticated_hls_plays_directly(self) -> None:
        t = get_adapter("hls").playback(ref("hls", "https://example.in/live/index.m3u8"))
        assert t.protocol == "hls"
        assert t.requires_relay is False
        assert t.url.endswith(".m3u8")

    def test_authenticated_hls_is_relayed_so_secrets_stay_server_side(self) -> None:
        """A credentialed URL in a browser is visible in devtools and history."""
        t = get_adapter("hls").playback(
            ref("hls", "https://example.in/live/index.m3u8", cred="site_a")
        )
        assert t.requires_relay is True
        assert t.protocol == "webrtc"

    def test_playback_url_never_contains_credentials(self, monkeypatch) -> None:
        monkeypatch.setenv("CAMERA_CRED_SITE_A_USER", "operator")
        monkeypatch.setenv("CAMERA_CRED_SITE_A_PASSWORD", "hunter2")
        for name, url in [
            ("rtsp", "rtsp://10.0.0.5:554/s1"),
            ("http", "https://live.example.in/stream/1"),
            ("hls", "https://example.in/live/index.m3u8"),
        ]:
            t = get_adapter(name).playback(ref(name, url, cred="site_a"))
            assert "hunter2" not in t.url
            assert "operator" not in t.url


class TestPathKeying:
    def test_path_prefers_external_ref(self) -> None:
        assert ref("rtsp", "rtsp://h/s").path == "cam-01"

    def test_path_falls_back_to_id_when_unreferenced(self) -> None:
        r = ref("rtsp", "rtsp://h/s", ext=None)
        assert r.path == r.id

    def test_path_is_the_published_one_when_we_already_serve_it(self) -> None:
        """A camera published to our own media server lives at that path.

        The feed simulator publishes from `cameras.stream_ref`, so keying on
        `external_ref` here would send the relay supervisor looking for a path
        nothing publishes — and it would start a second, redundant pull of a
        stream already in the media server.
        """
        r = CameraRef(id="id-a", external_ref="cam-01", name="A", adapter="rtsp",
                      stream_ref="rtsp://mediamtx:8554/nh48-km0")
        assert r.path == "nh48-km0"

    def test_a_foreign_rtsp_camera_still_keys_on_our_own_reference(self) -> None:
        r = CameraRef(id="id-a", external_ref="cam-01", name="A", adapter="rtsp",
                      stream_ref="rtsp://192.0.2.10:554/Streaming/Channels/101")
        assert r.path == "cam-01"

    def test_colliding_upstream_paths_do_not_collide_here(self) -> None:
        """Two feeds both served at /stream/1 must not share a relay path."""
        a = CameraRef(id="id-a", external_ref="sentinel-01", name="A", adapter="http",
                      stream_ref="https://host-a/stream/1")
        b = CameraRef(id="id-b", external_ref="sentinel-02", name="B", adapter="http",
                      stream_ref="https://host-b/stream/1")
        assert a.path != b.path


class TestCredentials:
    def test_unset_reference_resolves_to_nothing(self) -> None:
        assert resolve(None) is None
        assert resolve("never_configured_anywhere") is None

    def test_resolution_from_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("CAMERA_CRED_JUNAGADH_PTZ_USER", "operator")
        monkeypatch.setenv("CAMERA_CRED_JUNAGADH_PTZ_PASSWORD", "s3cret")
        c = resolve("junagadh_ptz")
        assert c == Credential("operator", "s3cret")

    def test_reference_is_normalised_to_an_env_key(self, monkeypatch) -> None:
        monkeypatch.setenv("CAMERA_CRED_SITE_7_NORTH_USER", "u")
        monkeypatch.setenv("CAMERA_CRED_SITE_7_NORTH_PASSWORD", "p")
        assert resolve("site-7.north") == Credential("u", "p")

    def test_repr_does_not_leak_the_password(self) -> None:
        """This object ends up in exception messages and log records."""
        assert "s3cret" not in repr(Credential("operator", "s3cret"))

    def test_apply_to_url_injects_userinfo(self) -> None:
        out = apply_to_url("rtsp://10.0.0.5:554/s1", Credential("operator", "s3cret"))
        assert out == "rtsp://operator:s3cret@10.0.0.5:554/s1"

    def test_special_characters_are_escaped(self) -> None:
        out = apply_to_url("rtsp://10.0.0.5/s", Credential("a@b", "p/w:d"))
        assert "a%40b" in out and "p%2Fw%3Ad" in out

    def test_apply_to_url_without_credentials_is_a_noop(self) -> None:
        assert apply_to_url("rtsp://10.0.0.5/s", None) == "rtsp://10.0.0.5/s"

    def test_redact_strips_userinfo(self) -> None:
        assert redact("rtsp://operator:s3cret@10.0.0.5:554/s1") == "rtsp://***@10.0.0.5:554/s1"

    def test_redact_leaves_clean_urls_alone(self) -> None:
        assert redact("rtsp://10.0.0.5:554/s1") == "rtsp://10.0.0.5:554/s1"


class TestProbeability:
    """Whether a source can be liveness-probed is the adapter's declaration.

    The health prober asks this rather than testing an adapter's name, so a new
    adapter type needs no edit there — the M2 acceptance rule applied to a
    module that is easy to forget about.
    """

    def test_network_sources_are_probeable(self) -> None:
        for name in ("rtsp", "http", "hls", "onvif"):
            assert get_adapter(name).probeable is True

    def test_a_clip_on_disk_is_not_probed(self) -> None:
        assert get_adapter("file").probeable is False

    def test_probeability_is_reported_in_describe(self) -> None:
        described = get_adapter("file").describe()
        assert described["probeable"] is False
        assert described["name"] == "file"
