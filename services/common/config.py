"""Single source of runtime configuration.

Everything comes from the environment (invariant 5 — no credential ever lands in
a file that git tracks). `.env.example` documents the full set; `.env` holds the
real values and is gitignored.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- database ---
    postgres_user: str = "cctv"
    postgres_password: str = ""
    postgres_db: str = "cctv"
    postgres_host: str = "db"
    postgres_port: int = 5432
    database_url: str | None = None

    # --- bus ---
    redpanda_brokers: str = "redpanda:9092"
    sightings_topic: str = "sightings"

    # --- media ---
    mediamtx_host: str = "mediamtx"
    mediamtx_rtsp_port: int = 8554
    mediamtx_webrtc_port: int = 8889
    mediamtx_hls_port: int = 8888
    mediamtx_api_port: int = 9997
    # How a browser reaches the media server. Overridden per deployment.
    mediamtx_public_url: str = "http://localhost:8889"
    #: Which transport the browser plays live video over: `webrtc` or `hls`.
    #:
    #: A deployment setting, not a preference. WebRTC is much lower latency and
    #: is right whenever the browser can reach the media server directly — a
    #: laptop, a LAN, a VM with a public address. But its *media* rides UDP, and
    #: an HTTP-only path in front of the platform — a Cloudflare Tunnel, a
    #: corporate proxy, the "restricted network" the integration contract names
    #: — carries the WHEP signalling perfectly and then silently starves the ICE
    #: negotiation. The session establishes and no frame ever paints.
    #:
    #: HLS is plain HTTP over the same origin, so it survives all of those, at
    #: the cost of a second or two of latency. Low-latency HLS is configured in
    #: `infra/mediamtx/mediamtx.yml`, which narrows that cost considerably.
    media_transport: str = "webrtc"

    # --- simulator ---
    sim_camera_count: int = 50
    sim_video_dir: str = "/data/test-videos"
    sim_offset_step_s: int = 90

    # --- routing ---
    osrm_url: str = "http://osrm:5000"

    # --- relay supervisor ---
    relay_url: str = "http://relay:8100"

    # --- api ---
    api_port: int = 8000
    api_cors_origins: str = "http://localhost:5173"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dsn(self) -> str:
        """libpq connection string. Prefers an explicit DATABASE_URL."""
        if self.database_url:
            return self.database_url
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rtsp_base(self) -> str:
        return f"rtsp://{self.mediamtx_host}:{self.mediamtx_rtsp_port}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mediamtx_public_base(self) -> str:
        """WebRTC base URL as the *browser* reaches it.

        Distinct from `mediamtx_host`, which is the compose-internal name the
        API and simulator use. A browser cannot resolve `mediamtx`, so playback
        URLs must be built from the externally reachable address.
        """
        return self.mediamtx_public_url.rstrip("/")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hls_playback(self) -> bool:
        """True when the browser should be handed an HLS playlist, not WHEP."""
        return self.media_transport.strip().lower() == "hls"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()

__all__ = ["Settings", "get_settings", "settings", "Field"]
