"""Relay supervisor — pulls a camera into the media server, on demand.

This is where the edge-first claim in README.md becomes real: video is not
continuously hauled to the centre, it is pulled only while somebody is watching.
A relay starts when an operator opens a camera and stops itself once nobody is
reading it.

Only cameras whose adapter reports `needs_relay` come through here, and only
those not already publishing. The 50 simulated cameras are published
continuously by the feed simulator, so opening one costs nothing; the 30
government feeds are RTSP on the organisers' own host and must be pulled and
republished before a browser can play them — a browser speaks WebRTC and HLS,
not RTSP.

The supervisor never invents a camera. It is given an id, looks it up in the
registry, and asks that camera's adapter where to read from (invariants 4 and 5).

Runs as its own service so the API container never handles media.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from services.adapters import CameraRef, get_adapter
from services.adapters.credentials import redact
from services.common.config import settings
from services.common.db import wait_for_db
from services.health.prober import fetch_mediamtx_paths

log = logging.getLogger("relay")

# How long a relay keeps running after its last reader disconnects. Long enough
# that flipping between cameras does not thrash ffmpeg, short enough that a
# forgotten tab does not pull video for hours.
IDLE_TIMEOUT_S = float(os.environ.get("RELAY_IDLE_TIMEOUT_S", "45"))

# A newly started relay is given this long to appear as ready before the caller
# is told it is still warming up. The M2 target is video within 3 seconds, so
# this must stay comfortably below that.
READY_WAIT_S = 2.0
READY_POLL_S = 0.1

REAPER_INTERVAL_S = 5.0

# Video codecs a browser will accept over WebRTC. Anything else has to be
# re-encoded before it can be watched, however healthy the stream is.
WEBRTC_VIDEO_CODECS = frozenset({"H264", "VP8", "VP9", "AV1"})

# Suffix for the browser-playable copy of a stream we had to re-encode. A
# separate path so the original is left untouched — ANPR reads the source at
# full quality, and only the operator's view pays for the transcode.
WEB_SUFFIX = "-web"

#: Window over which relay restarts are counted when judging a feed unstable.
UNSTABLE_WINDOW_S = 60.0

#: Restarts inside that window past which the feed is called unstable rather
#: than merely started. Two is deliberate: one restart is a stream that blinked
#: and recovered, which happens on any real network and should not be reported
#: as a fault.
UNSTABLE_RESTARTS = 2


@dataclass
class Relay:
    camera_id: str
    path: str
    proc: subprocess.Popen
    started_at: float
    last_reader_at: float = field(default_factory=time.monotonic)
    readers_seen: int = 0
    transcoded: bool = False

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None


class RelayState(BaseModel):
    camera_id: str
    path: str
    playback_url: str
    #: Which transport `playback_url` is for — `webrtc` or `hls`.
    #:
    #: Reported rather than assumed by the caller, because the relay is what
    #: builds the URL and only it knows which shape it built. The API forwards
    #: this to the player; getting it wrong means a WHEP handshake against a
    #: playlist, or the reverse, and neither fails in a way that names itself.
    playback_protocol: str = "webrtc"
    ready: bool
    relayed: bool
    started: bool
    detail: str
    transcoded: bool = False
    #: How many times this camera's relay has died and been restarted recently.
    #:
    #: `ready` alone was misleading and it showed in use. A government feed whose
    #: upstream resets the connection every couple of seconds leaves a publisher
    #: attached to the media server at the instant we poll, so the path reports
    #: ready, the browser establishes a WHEP session, and then nothing ever
    #: paints — which the operator sees as four reconnect attempts and a
    #: disconnect, with no indication that the fault is upstream. A stream that
    #: keeps dying is a different condition from one that is down and from one
    #: that is fine, and the platform should be able to say which.
    restarts_recent: int = 0


def _load_camera(camera_id: str) -> CameraRef:
    """Resolve a camera from the registry. Never from a cache or a constant."""
    with psycopg.connect(settings.dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id::text, external_ref, name, adapter::text, stream_ref,"
            " credential_ref, kind::text FROM cameras WHERE id = %s",
            (camera_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="camera not found")
    return CameraRef(
        id=row[0], external_ref=row[1], name=row[2], adapter=row[3],
        stream_ref=row[4], credential_ref=row[5], kind=row[6],
    )


def webrtc_playable(tracks: list[str] | None) -> bool:
    """True when a browser can play this stream over WebRTC as it stands.

    MediaMTX reports track codecs by name. Anything outside the WebRTC set —
    the estate's H.265 and MPEG-4 cameras — connects, delivers a handful of
    packets and is then dropped by the server, which in the browser looks like
    a video that never paints. Better to know before offering it.
    """
    video = [t for t in (tracks or []) if t.upper() not in {"OPUS", "MPEG4-AUDIO", "G711", "AAC"}]
    return bool(video) and all(t.upper() in WEBRTC_VIDEO_CODECS for t in video)


def _ffmpeg_command(source: str, target: str, transcode: bool = False) -> list[str]:
    """Pull `source` and republish it to `target` as RTSP.

    Stream copy by default: the government feeds are already h264, so
    re-encoding 31 of them would burn the box to achieve nothing. Reconnect
    flags matter because the upstream returns 503 while a stream warms up.

    `transcode` is for the cameras a browser cannot play at all. Baseline
    profile with no B-frames, because WebRTC carries neither H.265 nor an
    H.264 stream containing B-frames.
    """
    codec: list[str] = ["-c", "copy"]
    if transcode:
        codec = [
            "-c:v", "libx264",
            "-profile:v", "baseline",
            "-level", "3.1",
            "-bf", "0",
            # Encoding happens only while somebody is watching, so latency
            # matters and long-run compression efficiency does not.
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-g", "30",
            "-an",
        ]
    # The reconnect options belong to ffmpeg's HTTP reader and `-rtsp_transport`
    # to its RTSP one; ffmpeg rejects the wrong family outright rather than
    # ignoring it ("Option reconnect not found"), so the input options are
    # chosen from the source's scheme.
    if source.startswith(("http://", "https://")):
        input_opts = [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_on_http_error", "5xx",
            "-reconnect_delay_max", "5",
            "-rw_timeout", "15000000",
        ]
    else:
        # No read timeout: `-stimeout` was removed in ffmpeg 6 and the source
        # here is our own media server on the same network.
        input_opts = ["-rtsp_transport", "tcp"]

    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        *input_opts,
        "-i", source,
        *codec,
        # Looping sources restart their timestamps, which the muxer rejects as
        # non-monotonic unless they are regenerated.
        "-fflags", "+genpts",
        "-f", "rtsp", "-rtsp_transport", "tcp",
        target,
    ]


class Supervisor:
    def __init__(self) -> None:
        self.relays: dict[str, Relay] = {}
        self.lock = threading.Lock()
        self._stop = threading.Event()
        #: camera_id -> monotonic times this camera's relay process exited.
        #: Trimmed to the window below, so it cannot grow on a long-running
        #: supervisor watching a permanently broken feed.
        self.exits: dict[str, list[float]] = {}
        self.api_base = f"http://{settings.mediamtx_host}:{settings.mediamtx_api_port}"

    # --- media server state ---

    def _paths(self) -> dict:
        paths, err = fetch_mediamtx_paths(self.api_base)
        if err:
            log.warning("media server API unreachable: %s", err)
        return paths

    def _is_published(self, path: str, paths: dict | None = None) -> bool:
        info = (paths if paths is not None else self._paths()).get(path)
        return bool(info and info.get("ready"))

    # --- lifecycle ---

    def ensure(self, camera_id: str) -> RelayState:
        state = self._ensure(camera_id)
        # Stamped in one place rather than on each of the six return paths,
        # which is how one of them would eventually be missed.
        state.restarts_recent = self.restarts_recent(camera_id)
        if state.restarts_recent >= UNSTABLE_RESTARTS:
            state.detail = (
                f"{state.detail}; upstream unstable — the relay has restarted "
                f"{state.restarts_recent} times in the last minute"
            )
        return state

    @staticmethod
    def _playback(path: str) -> tuple[str, str]:
        """Where the browser plays `path`, and over which transport.

        In one place on purpose. `ensure` already notes that this class has six
        return paths and that "this is how one of them would eventually be
        missed" — and when HLS was added, one of them was: the *already
        publishing* branch kept handing back the adapter's WebRTC URL, so on a
        hosted instance every simulated camera resolved to a WHEP endpoint that
        the player then tried to fetch as a playlist.

        WHEP takes the bare path and the player appends `/whep`; HLS needs the
        playlist itself. Building both here means the two cannot drift again.
        """
        base = settings.mediamtx_public_base
        if settings.hls_playback:
            return f"{base}/{path}/index.m3u8", "hls"
        return f"{base}/{path}", "webrtc"

    def _ensure(self, camera_id: str) -> RelayState:
        camera = _load_camera(camera_id)
        adapter = get_adapter(camera.adapter)
        target = adapter.playback(camera)

        if not target.requires_relay:
            return RelayState(
                camera_id=camera.id, path=camera.path, playback_url=target.url,
                ready=True, relayed=False, started=False,
                detail=target.detail or "played directly",
            )

        paths = self._paths()
        source_info = paths.get(camera.path) or {}
        source_live = bool(source_info.get("ready"))

        # Already publishing — either the simulator's doing, an encoder pushing
        # to us, or a relay we started earlier — and in a codec a browser can
        # play. Opening it costs nothing.
        if source_live and webrtc_playable(source_info.get("tracks")):
            self._touch(camera_id)
            playback_url, playback_protocol = self._playback(camera.path)
            return RelayState(
                camera_id=camera.id, path=camera.path, playback_url=playback_url,
                playback_protocol=playback_protocol,
                ready=True, relayed=True, started=False,
                detail="already publishing",
            )

        if source_live:
            # The stream is healthy, the browser simply cannot decode it. Re-encode
            # into a second path and leave the original alone: ANPR keeps reading
            # the source at full quality, and only a watched camera costs a CPU.
            play_path = camera.path + WEB_SUFFIX
            source = f"{settings.rtsp_base}/{camera.path}"
            transcode = True
            reason = f"re-encoded for the browser ({', '.join(source_info.get('tracks') or [])})"
        else:
            play_path = camera.path
            source = adapter.ingest_url(camera)
            transcode = False
            reason = "relay started"

        playback_url, playback_protocol = self._playback(play_path)

        if self._is_published(play_path, paths):
            self._touch(camera_id)
            return RelayState(
                camera_id=camera.id, path=play_path, playback_url=playback_url,
                playback_protocol=playback_protocol,
                ready=True, relayed=True, started=False, transcoded=transcode,
                detail="already publishing",
            )

        started = self._start(camera, source, play_path, transcode)

        # Give it a moment to appear, so the common case is a single request
        # that returns a playable URL rather than a poll loop in the client.
        deadline = time.monotonic() + READY_WAIT_S
        while time.monotonic() < deadline:
            if self._is_published(play_path):
                return RelayState(
                    camera_id=camera.id, path=play_path, playback_url=playback_url,
                    playback_protocol=playback_protocol,
                    ready=True, relayed=True, started=started, transcoded=transcode,
                    detail=reason,
                )
            time.sleep(READY_POLL_S)

        return RelayState(
            camera_id=camera.id, path=play_path, playback_url=playback_url,
            playback_protocol=playback_protocol,
            ready=False, relayed=True, started=started, transcoded=transcode,
            detail="relay starting; retry shortly",
        )

    def _note_exit(self, camera_id: str, now: float) -> None:
        """Record that this camera's relay died. Caller holds the lock."""
        history = [t for t in self.exits.get(camera_id, []) if now - t < UNSTABLE_WINDOW_S]
        history.append(now)
        self.exits[camera_id] = history

    def restarts_recent(self, camera_id: str, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        with self.lock:
            history = [t for t in self.exits.get(camera_id, []) if now - t < UNSTABLE_WINDOW_S]
            self.exits[camera_id] = history
            return len(history)

    def _touch(self, camera_id: str) -> None:
        """Mark a relay as still wanted, so the reaper leaves it alone."""
        with self.lock:
            if camera_id in self.relays:
                self.relays[camera_id].last_reader_at = time.monotonic()

    def _start(self, camera: CameraRef, source: str, play_path: str, transcode: bool) -> bool:
        with self.lock:
            existing = self.relays.get(camera.id)
            if existing and existing.alive:
                existing.last_reader_at = time.monotonic()
                return False

            target = f"{settings.rtsp_base}/{play_path}"
            # Logged redacted: the source may carry resolved credentials.
            log.info(
                "relay start %s: %s -> %s%s",
                play_path, redact(source), target, " (transcoding)" if transcode else "",
            )

            proc = subprocess.Popen(
                _ffmpeg_command(source, target, transcode=transcode),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            self.relays[camera.id] = Relay(
                camera_id=camera.id, path=play_path, proc=proc,
                started_at=time.monotonic(), transcoded=transcode,
            )
            return True

    def stop(self, camera_id: str) -> bool:
        with self.lock:
            relay = self.relays.pop(camera_id, None)
        if relay is None:
            return False
        relay.proc.terminate()
        try:
            relay.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            relay.proc.kill()
        log.info("relay stopped %s", relay.path)
        return True

    # --- reaper ---

    def reap(self) -> None:
        """Stop relays nobody is reading, and clear out dead ones."""
        paths = self._paths()
        now = time.monotonic()

        for camera_id, relay in list(self.relays.items()):
            if not relay.alive:
                stderr = b""
                if relay.proc.stderr is not None:
                    with contextlib.suppress(ValueError, OSError):
                        stderr = relay.proc.stderr.read() or b""
                log.warning(
                    "relay %s exited rc=%s: %s",
                    relay.path, relay.proc.returncode,
                    stderr.decode(errors="replace").strip()[-300:] or "(no output)",
                )
                with self.lock:
                    self.relays.pop(camera_id, None)
                    self._note_exit(camera_id, now)
                continue

            readers = len((paths.get(relay.path) or {}).get("readers", []))
            if readers > 0:
                relay.last_reader_at = now
                relay.readers_seen = max(relay.readers_seen, readers)
                continue

            # Never reap a relay that has not yet had a chance to be watched:
            # the operator's player takes a moment to connect after we return.
            if relay.readers_seen == 0 and now - relay.started_at < IDLE_TIMEOUT_S:
                continue

            if now - relay.last_reader_at > IDLE_TIMEOUT_S:
                log.info("relay %s idle for %.0fs, stopping", relay.path, IDLE_TIMEOUT_S)
                self.stop(camera_id)

    def run_reaper(self) -> None:
        while not self._stop.wait(REAPER_INTERVAL_S):
            try:
                self.reap()
            except Exception:  # noqa: BLE001 - one bad pass must not kill the reaper
                log.exception("reaper pass failed")

    def shutdown(self) -> None:
        self._stop.set()
        for camera_id in list(self.relays):
            self.stop(camera_id)


supervisor = Supervisor()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    )
    wait_for_db()
    threading.Thread(target=supervisor.run_reaper, daemon=True).start()
    log.info("relay supervisor ready (idle timeout %.0fs)", IDLE_TIMEOUT_S)
    yield
    # Every relay is a live ffmpeg holding an upstream connection open. Leaving
    # them behind on shutdown would keep pulling video for a service that no
    # longer exists.
    supervisor.shutdown()


app = FastAPI(title="Relay supervisor", version="0.1.0", lifespan=lifespan)


@app.post("/relay/{camera_id}", response_model=RelayState, summary="Ensure a camera is relayed")
def ensure_relay(camera_id: str) -> RelayState:
    return supervisor.ensure(camera_id)


@app.delete("/relay/{camera_id}", summary="Stop a relay")
def stop_relay(camera_id: str) -> dict[str, bool]:
    return {"stopped": supervisor.stop(camera_id)}


@app.get("/relay", summary="List active relays")
def list_relays() -> dict[str, object]:
    with supervisor.lock:
        return {
            "count": len(supervisor.relays),
            "relays": [
                {
                    "camera_id": r.camera_id,
                    "path": r.path,
                    "alive": r.alive,
                    "transcoded": r.transcoded,
                    "uptime_s": round(time.monotonic() - r.started_at, 1),
                    "readers_seen": r.readers_seen,
                }
                for r in supervisor.relays.values()
            ],
        }


@app.get("/health", summary="Liveness")
def health() -> dict[str, object]:
    return {"status": "ok", "relays": len(supervisor.relays)}
