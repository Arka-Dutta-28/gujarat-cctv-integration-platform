"""Camera adapter interface.

The adapter spine (Model 3) is what makes the estate's heterogeneity someone
else's problem. Every camera, whether RTSP, progressive HTTP, HLS, a file on
disk or an ONVIF device, is reduced to the same question: how do I get a
playable stream for this camera?

The rule that keeps this honest: adding a new adapter type must require no
changes outside its own module. No `if adapter == ...` chain anywhere else, no
edit to a factory, no new branch in the API. Adapters self-register by
decorating themselves, and the registry's `adapter` column selects one by name.
That rule is part of the M2 acceptance test, not just an aspiration.

Credentials never appear in stream_ref; the API rejects them there. An adapter
that needs them resolves credential_ref through the vault at connect time, so a
password lives in exactly one place and never in a database row, an API response
or a log line.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urlparse

from services.common.config import settings

# Hostnames that mean "our own media server", however the URL spells it. Kept
# in step with the health prober, which decides the same question when it works
# out whether a camera is proxied by us or probed directly.
_OWN_MEDIA_HOSTS = {settings.mediamtx_host, "mediamtx", "localhost", "127.0.0.1"}

__all__ = [
    "CameraRef",
    "PlaybackTarget",
    "CameraAdapter",
    "register_adapter",
    "get_adapter",
    "available_adapters",
]


@dataclass(frozen=True)
class CameraRef:
    """What an adapter is told about a camera. Comes from the registry, always.

    `endpoints` and `stream_properties` arrived with the live grid. The
    integration reference is explicit that a camera publishes several
    transports and that which of them is reachable depends on the network, and
    equally explicit that the grid is not uniform — mixed codecs, resolutions
    and frame rates. Both are facts about a camera, so both live in the
    registry and travel with the camera reference rather than being rediscovered
    by whatever happens to open the stream.
    """

    id: str
    external_ref: str | None
    name: str
    adapter: str
    stream_ref: str
    credential_ref: str | None = None
    kind: str = "unknown"
    #: Every transport the upstream catalogue published for this camera, as
    #: ``[{"protocol": "rtsp", "url": "..."}]``. Empty for cameras onboarded
    #: before the catalogue existed, which fall back to `stream_ref`.
    #: Excluded from equality and hashing: a camera's identity is its id,
    #: not the transports it happened to publish this morning — and a dict
    #: field would make an otherwise hashable reference unhashable.
    endpoints: tuple[dict[str, str], ...] = field(default=(), compare=False)
    #: Codec, container, width, height, declared_fps, bitrate_kbps — whatever
    #: the catalogue reported. `declared_fps` is for comparison only.
    stream_properties: dict[str, Any] = field(default_factory=dict, compare=False)

    def endpoint_urls(self, protocols: tuple[str, ...]) -> list[tuple[str, str]]:
        """(protocol, url) pairs from the registry, in the order asked for.

        Endpoints the platform does not know how to read are simply absent from
        `protocols`, so adding a transport is a change to the caller's
        preference list and to nothing here.
        """
        by_protocol: dict[str, str] = {}
        for entry in self.endpoints:
            protocol, url = entry.get("protocol"), entry.get("url")
            if protocol and url and protocol not in by_protocol:
                by_protocol[protocol] = url
        return [(p, by_protocol[p]) for p in protocols if p in by_protocol]

    @property
    def path(self) -> str:
        """Media-server path this camera's video lives at.

        A camera whose `stream_ref` already points at our own media server is
        *published there by something else* — the feed simulator, or an encoder
        pushing to us — so its path is the one in that URL. Anything else is
        pulled by us, and we choose the path: the platform's own identifier,
        never something parsed out of a foreign URL, so two cameras whose
        upstream paths collide (`/stream/1` on two different hosts) cannot
        overwrite each other's stream.
        """
        parsed = urlparse(self.stream_ref or "")
        if parsed.scheme in {"rtsp", "rtsps"} and parsed.hostname in _OWN_MEDIA_HOSTS:
            own = parsed.path.lstrip("/")
            if own:
                return own
        return self.external_ref or self.id


@dataclass
class PlaybackTarget:
    """Where a browser should go to watch this camera, and how.

    `ready` false means the stream is being prepared; the caller should retry
    rather than treat it as an error, because pull-on-demand sources take a
    moment to start.
    """

    camera_id: str
    protocol: str
    url: str
    ready: bool = True
    requires_relay: bool = False
    detail: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class CameraAdapter(ABC):
    """One way of getting video out of a camera.

    Subclasses set `name` to the value used in the registry's `adapter` column
    and decorate themselves with `@register_adapter`.
    """

    #: Registry value this adapter serves.
    name: ClassVar[str] = ""

    #: True when video must be pulled by us and republished before a browser can
    #: play it — anything a browser cannot open directly.
    needs_relay: ClassVar[bool] = False

    #: False when a liveness probe is meaningless for this source. The health
    #: prober asks the adapter rather than testing its name, so a new adapter
    #: that cannot be probed needs no change in the prober.
    probeable: ClassVar[bool] = True

    @abstractmethod
    def playback(self, camera: CameraRef) -> PlaybackTarget:
        """Return where a browser can watch this camera."""

    @abstractmethod
    def probe_url(self, camera: CameraRef) -> str:
        """URL a health probe or an ANPR worker should read from."""

    def ingest_url(self, camera: CameraRef) -> str:
        """URL ffmpeg should read when pulling this camera for analytics.

        Defaults to the probe URL; adapters override when analytics need
        different parameters from a liveness check.
        """
        return self.probe_url(camera)

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "needs_relay": self.needs_relay,
                "probeable": self.probeable}


# --- registry ----------------------------------------------------------
# Populated by decoration at import time. Nothing else enumerates adapters, so
# a new one is added by writing its module and importing it — never by editing
# a list here.

_ADAPTERS: dict[str, CameraAdapter] = {}


def register_adapter(cls: type[CameraAdapter]) -> type[CameraAdapter]:
    """Class decorator: make this adapter available under its `name`."""
    if not cls.name:
        raise ValueError(f"{cls.__name__} must set a `name`")
    if cls.name in _ADAPTERS:
        raise ValueError(f"adapter {cls.name!r} is already registered")
    _ADAPTERS[cls.name] = cls()
    return cls


def get_adapter(name: str) -> CameraAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise LookupError(
            f"no adapter for {name!r}; registered: {sorted(_ADAPTERS)}"
        ) from None


def available_adapters() -> dict[str, CameraAdapter]:
    return dict(_ADAPTERS)
