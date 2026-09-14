"""Adapter implementations, one per delivery mechanism.

Everything a given camera type needs lives in its own class here. Adding a new
type means adding a class with `@register_adapter` and importing this module —
nothing outside changes, which is what the M2 acceptance test checks.

Browsers cannot play RTSP, and they cannot play an unbounded progressive MP4
usefully either, so most adapters set `needs_relay` and hand playback to
MediaMTX over WebRTC. `file` and `hls` are the exceptions: a browser can open
those directly.
"""

from __future__ import annotations

from services.adapters.base import (
    CameraAdapter,
    CameraRef,
    PlaybackTarget,
    register_adapter,
)
from services.adapters.credentials import apply_to_url, resolve
from services.common.config import settings

__all__ = ["RtspAdapter", "HttpAdapter", "HlsAdapter", "FileAdapter", "OnvifAdapter"]


def _webrtc_url(camera: CameraRef) -> str:
    """Where the browser plays a relayed camera from."""
    base = settings.mediamtx_public_base
    return f"{base}/{camera.path}"


class _RelayedAdapter(CameraAdapter):
    """Shared behaviour for sources a browser cannot open directly.

    We pull the camera and republish it into MediaMTX; the browser then plays
    WebRTC from a single, uniform place regardless of what the camera speaks.
    That uniformity is the reason the live-view UI has no per-protocol code.
    """

    needs_relay = True

    def playback(self, camera: CameraRef) -> PlaybackTarget:
        return PlaybackTarget(
            camera_id=camera.id,
            protocol="webrtc",
            url=_webrtc_url(camera),
            requires_relay=True,
            detail=f"{self.name} source relayed into the media server",
        )


@register_adapter
class RtspAdapter(_RelayedAdapter):
    """Standard IP camera. The simulated NH-48 farm is all of these."""

    name = "rtsp"

    def probe_url(self, camera: CameraRef) -> str:
        return apply_to_url(camera.stream_ref, resolve(camera.credential_ref))


@register_adapter
class HttpAdapter(_RelayedAdapter):
    """Progressive HTTP byte-range video.

    What the 31 government evaluation feeds actually are: `delivery:
    "progressive"`, containers mp4/mkv/avi, `hls_url` null. A browser can open
    such a URL, but it behaves as an unbounded download rather than a live
    stream — no seeking to live edge, and a tab that buffers forever — so it is
    relayed like any other pull source.
    """

    name = "http"

    def probe_url(self, camera: CameraRef) -> str:
        return apply_to_url(camera.stream_ref, resolve(camera.credential_ref))


@register_adapter
class HlsAdapter(CameraAdapter):
    """HLS playlist. Browsers play this natively, so no relay is needed."""

    name = "hls"
    needs_relay = False

    def playback(self, camera: CameraRef) -> PlaybackTarget:
        # Deliberately not credential-injected: this URL is handed to a browser,
        # and a URL containing a password would be visible in devtools, history
        # and any screen recording of the demo. An authenticated HLS source is
        # relayed instead, so the secret stays server-side.
        if camera.credential_ref:
            return PlaybackTarget(
                camera_id=camera.id,
                protocol="webrtc",
                url=_webrtc_url(camera),
                requires_relay=True,
                detail="authenticated HLS is relayed so credentials stay server-side",
            )
        return PlaybackTarget(
            camera_id=camera.id,
            protocol="hls",
            url=camera.stream_ref,
            detail="played directly by the browser",
        )

    def probe_url(self, camera: CameraRef) -> str:
        return apply_to_url(camera.stream_ref, resolve(camera.credential_ref))


@register_adapter
class FileAdapter(CameraAdapter):
    """A clip on disk.

    Kept working on purpose: it is the fallback demo path if the government
    feeds are unavailable on the day (build-plan §7, known risks).
    """

    name = "file"
    needs_relay = False
    # A clip on disk is either there or it is not; there is no liveness to
    # sample, and probing it every few seconds would only measure the disk.
    probeable = False

    def playback(self, camera: CameraRef) -> PlaybackTarget:
        return PlaybackTarget(
            camera_id=camera.id,
            protocol="file",
            url=camera.stream_ref,
            detail="local clip; served by the API rather than the media server",
        )

    def probe_url(self, camera: CameraRef) -> str:
        return camera.stream_ref


@register_adapter
class OnvifAdapter(_RelayedAdapter):
    """ONVIF device.

    ONVIF is a discovery and control protocol; the media itself is RTSP obtained
    from the device's media service. Until that handshake is implemented, the
    registry's `stream_ref` is treated as the already-resolved RTSP URL, which
    is how most ONVIF cameras are onboarded in practice anyway.
    """

    name = "onvif"

    def probe_url(self, camera: CameraRef) -> str:
        return apply_to_url(camera.stream_ref, resolve(camera.credential_ref))

    def describe(self) -> dict[str, object]:
        return {
            **super().describe(),
            "note": "device discovery not implemented; stream_ref used as resolved RTSP",
        }
