"""The stream ladder a worker tries, for a camera that needs a login."""

from __future__ import annotations

from types import SimpleNamespace

from services.adapters import CameraRef
from services.anpr.worker import CameraWorker


def _camera(credential_ref: str | None) -> CameraRef:
    return CameraRef(
        id="c1", external_ref="sentinel-cam01", name="01", adapter="rtsp",
        stream_ref="rtsp://grid.example:8554/stream/cam01", credential_ref=credential_ref,
        endpoints=({"protocol": "rtsp", "url": "rtsp://grid.example:8554/stream/cam01"},
                   {"protocol": "hls", "url": "https://grid.example/cam01/index.m3u8"}),
    )


def test_the_first_rtsp_attempt_carries_the_login(monkeypatch) -> None:
    monkeypatch.setenv("CAMERA_CRED_SENTINEL_USER", "someone@example.org")
    monkeypatch.setenv("CAMERA_CRED_SENTINEL_PASSWORD", "ABCD-EFGH-IJKL")
    worker = SimpleNamespace(camera=_camera("sentinel"), prefer_relay=False)
    ladder = CameraWorker.candidates(worker)
    assert ladder[0].protocol == "rtsp"
    assert ladder[0].url.startswith("rtsp://someone%40example.org:ABCD-EFGH-IJKL@grid.example")
    urls = [c.url for c in ladder]
    assert len(urls) == len(set(urls)), "the adapter fallback must not repeat the same address"
    assert not any("@" in c.url for c in ladder if c.protocol != "rtsp")


def test_a_camera_without_a_login_is_untouched(monkeypatch) -> None:
    ladder = CameraWorker.candidates(SimpleNamespace(camera=_camera(None), prefer_relay=False))
    assert ladder[0].url == "rtsp://grid.example:8554/stream/cam01"
