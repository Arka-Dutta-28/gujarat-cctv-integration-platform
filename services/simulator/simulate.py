"""Feed simulator: publishes the registry's cameras as live RTSP streams.

To the adapter layer these are indistinguishable from real cameras, which is the
point. The RTSP path is genuinely exercised rather than stubbed, so when the
government feeds arrive the change is cameras.stream_ref and nothing else.

The simulator is told nothing about which cameras exist. It reads the registry
(invariant 4), joins camera_sim_config for the harness parameters, and publishes
whatever it finds.

Two things worth knowing before editing this file.

1. Variants are transcoded once and cached, then published with `-c copy`. The
   farm is deliberately heterogeneous, six encoding profiles across 50 cameras,
   but re-encoding 50 live streams would burn every core on the box and tell us
   nothing. Transcoding each (clip, profile) pair once and then stream-copying
   it costs almost nothing per stream, so the measured limit is the platform's,
   not ffmpeg's.

2. Playback offset is a seek, applied modulo the clip duration. Camera N starts
   offset_s into its clip so a vehicle reaches successive cameras at successive
   times. Offsets are derived from real inter-camera distance, so they routinely
   exceed a short clip's length, and taking them modulo the duration keeps the
   seek legal.

Usage:
    python -m services.simulator.simulate
    python -m services.simulator.simulate --dry-run
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import psycopg

from services.common.config import settings
from services.common.db import wait_for_db

log = logging.getLogger("simulator")

# Where transcoded profile variants are cached between runs.
VARIANT_DIR = Path(os.environ.get("SIM_VARIANT_DIR", "/data/sim-cache"))

# A stream that dies is restarted with linear backoff up to this ceiling. A
# camera that keeps failing should stay visible as a restarting stream rather
# than vanish — that is what a flapping real camera looks like too.
RESTART_BACKOFF_S = 2.0
RESTART_BACKOFF_MAX_S = 30.0

# Starting 50 ffmpeg processes in the same instant makes the media server
# reject connections and produces a misleading "the platform cannot cope".
STAGGER_S = 0.12


@dataclass(frozen=True)
class SimCamera:
    """One row of the registry joined with its simulation parameters."""

    camera_id: str
    external_ref: str
    name: str
    stream_ref: str
    source_file: str
    offset_s: int
    profile: str
    width: int | None
    height: int | None
    fps: float | None
    codec: str | None
    extra_args: list[str]

    @property
    def path(self) -> str:
        """RTSP path this camera publishes on, taken from the registry URL.

        Derived rather than assumed: the registry is what says where a camera
        lives, so a hand-edited stream_ref moves the stream with no code change.
        """
        return urlparse(self.stream_ref).path.lstrip("/") or self.external_ref

    @property
    def variant_key(self) -> str:
        return f"{Path(self.source_file).stem}__{self.profile}"


#: Which cameras to publish, as comma-separated `external_ref` patterns where
#: `*` matches anything. Empty — the default — publishes the whole farm.
#:
#: This exists because of a timing property that is easy to miss and impossible
#: to work around downstream. The corridor's journey depends on six cameras
#: showing one long clip at offsets 238 s apart, and those offsets are only
#: meaningful **relative to a common start**. Publishing fifty streams means
#: fifty ffmpeg processes each seeking deep into a 1,548 s file, and they do not
#: come up together: measured 7 Sep, cam-21's stream started at 20:21:47 and
#: cam-20's at 20:28:01 — **six minutes apart**, against a stagger of four.
#:
#: The visible symptom is a planted vehicle that appears at cam-20 and cam-22
#: 85 seconds apart when the corridor implies 476, which the journey check
#: correctly calls implausible. It reproduced at 84 s, 85 s and 85 s across
#: three restarts and a clip regeneration — reproducible, so not jitter in the
#: usual sense, but a stable consequence of start order.
#:
#: Nine streams come up inside a few seconds of each other, so the stagger
#: holds. Publishing fewer cameras is the only lever that fixes this; no change
#: to the clip or to the offsets can, because both assume the common start that
#: fifty concurrent seeks destroy.
CAMERA_REFS = os.environ.get("SIM_CAMERA_REFS", "").strip()


def camera_patterns(spec: str) -> list[str]:
    """Split the publish spec into SQL LIKE patterns. Empty list means no filter."""
    return [p.strip().replace("*", "%") for p in spec.split(",") if p.strip()]


def load_cameras(dsn: str, *, refs: str | None = None) -> list[SimCamera]:
    """Read the farm from the registry. The simulator invents no cameras."""
    sql = """
        SELECT c.id::text        AS camera_id,
               c.external_ref,
               c.name,
               c.stream_ref,
               s.source_file,
               s.offset_s,
               s.profile,
               s.width, s.height, s.fps, s.codec,
               COALESCE(s.extra_args, ARRAY[]::TEXT[]) AS extra_args
          FROM cameras c
          JOIN camera_sim_config s ON s.camera_id = c.id
         WHERE c.status <> 'decommissioned'
           AND s.source_file <> ''
    """
    patterns = camera_patterns(CAMERA_REFS if refs is None else refs)
    params: list = []
    if patterns:
        sql += " AND c.external_ref LIKE ANY(%s)"
        params.append(patterns)

    # Cameras that share a source file are the only ones with a timing
    # relationship to each other: the corridor's journey is six cameras showing
    # one clip at offsets 238 s apart, and those offsets mean nothing unless the
    # six streams start together. So start each shared-clip group *contiguously*,
    # and the biggest group first.
    #
    # Ordering by `external_ref` alone put the corridor twentieth in a queue of
    # fifty ffmpeg processes each seeking deep into a 1,548 s file. Measured
    # 7 Sep: cam-21's stream came up at 20:21:47 and cam-20's at 20:28:01, six
    # minutes apart against a four-minute stagger — so the planted vehicle
    # appeared at cam-20 and cam-22 85 s apart where the corridor implies 476,
    # and the journey check correctly called it impossible. It reproduced at
    # 84 s, 85 s and 85 s across three restarts.
    #
    # This costs nothing: the same fifty streams start, in a different order.
    sql += """
         ORDER BY count(*) OVER (PARTITION BY s.source_file) DESC,
                  s.source_file,
                  c.external_ref
    """

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params or None)
        cols = [d.name for d in cur.description]
        return [SimCamera(**dict(zip(cols, row, strict=True))) for row in cur.fetchall()]


def probe_duration_s(path: Path) -> float:
    """Clip length in seconds, via ffprobe. Zero if it cannot be determined."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        log.warning("ffprobe failed for %s: %s", path.name, proc.stderr.strip()[:200])
        return 0.0
    try:
        return float(json.loads(proc.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return 0.0


def build_variant(cam: SimCamera, source_dir: Path, out_dir: Path) -> Path | None:
    """Transcode this camera's clip into its profile, once, and cache it.

    Returns the cached path, or None if the source clip is missing.
    """
    source = source_dir / cam.source_file
    if not source.is_file():
        log.error("camera %s: source clip %s not found", cam.external_ref, source)
        return None

    out = out_dir / f"{cam.variant_key}.mp4"
    # Existence is not freshness. The cache key is the clip name plus the
    # profile, so regenerating a clip under the same name leaves the old variant
    # in place forever — and a clip that was still being written when the
    # transcode ran leaves a truncated one. Both were hit while building the M4
    # journey corridor, and the second is the nastier: the stream publishes, the
    # camera looks healthy, and the content is simply wrong. Comparing mtimes
    # costs a stat and removes a whole class of demo-day confusion.
    if out.is_file() and out.stat().st_size > 0:
        if out.stat().st_mtime >= source.stat().st_mtime:
            return out
        log.info(
            "camera %s: %s changed since it was transcoded; rebuilding variant",
            cam.external_ref, cam.source_file,
        )
        out.unlink(missing_ok=True)

    encoder = {"h264": "libx264", "hevc": "libx265", "mpeg4": "mpeg4"}.get(
        cam.codec or "h264", "libx264"
    )
    vf = []
    if cam.width and cam.height:
        vf.append(f"scale={cam.width}:{cam.height}")

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    if cam.fps:
        cmd += ["-r", str(cam.fps)]
    cmd += [
        "-c:v", encoder,
        "-preset", "veryfast" if encoder in {"libx264", "libx265"} else "medium",
        "-pix_fmt", "yuv420p",
        # Two-second GOP: an RTSP subscriber gets a picture quickly, and the M3
        # decoder has frequent seek points.
        "-g", str(int((cam.fps or 15) * 2)),
        "-an",
    ]
    if encoder == "libx264":
        # No B-frames, and a profile WebRTC will actually carry.
        #
        # This is not a quality preference. MediaMTX refuses an H.264 stream
        # containing B-frames with "WebRTC doesn't support H264 streams with
        # B-frames": the session is established, a few packets arrive, and the
        # server closes it. The failure surfaces in the browser as a video that
        # connects and never paints — which is exactly how it presented, and it
        # would have taken the live view down in front of evaluators.
        #
        # Real CCTV encoders overwhelmingly emit baseline or main without
        # B-frames for the same reason, so this also makes the simulated farm a
        # more faithful stand-in for the estate.
        cmd += ["-profile:v", "baseline", "-level", "3.1", "-bf", "0"]
    if encoder == "libx265":
        # Without this, hvc1/hev1 tagging trips some players on playback.
        cmd += ["-tag:v", "hvc1"]
    cmd += [*cam.extra_args, str(out)]

    log.info("transcoding %s -> %s", cam.source_file, out.name)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        log.error("transcode failed for %s:\n%s", cam.external_ref, proc.stderr[-800:])
        out.unlink(missing_ok=True)
        return None
    return out


def publish_command(cam: SimCamera, variant: Path, duration_s: float, rtsp_base: str) -> list[str]:
    """ffmpeg command that loops `variant` onto this camera's RTSP path.

    `-c copy` is what keeps 50 concurrent streams cheap; all the encoding work
    already happened in build_variant.
    """
    # Offsets come from real corridor distances and routinely exceed a short
    # clip, so wrap them rather than seeking past the end.
    seek = (cam.offset_s % duration_s) if duration_s > 0 else 0.0

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if seek > 0:
        cmd += ["-ss", f"{seek:.3f}"]
    cmd += [
        "-stream_loop", "-1",
        # Pace output at real time; without it ffmpeg races through the file.
        "-re",
        "-i", str(variant),
        # Looping restarts the source timestamps, which the muxer rejects as
        # non-monotonic unless they are regenerated.
        "-fflags", "+genpts",
        "-c", "copy",
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        f"{rtsp_base}/{cam.path}",
    ]
    return cmd


class StreamProcess:
    """Supervises one camera's ffmpeg process, restarting it if it dies."""

    def __init__(self, cam: SimCamera, cmd: list[str]) -> None:
        self.cam = cam
        self.cmd = cmd
        self.proc: subprocess.Popen | None = None
        self.restarts = 0
        self.started_at: float | None = None
        self._backoff = RESTART_BACKOFF_S
        self._next_attempt = 0.0

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.started_at = time.monotonic()

    def poll(self, now: float) -> None:
        """Restart the stream if it has exited, respecting backoff."""
        if self.alive:
            # A stream that has held up for a while has recovered; reset backoff
            # so one bad patch does not permanently slow its restarts.
            if self.started_at and now - self.started_at > 60:
                self._backoff = RESTART_BACKOFF_S
            return

        if self.proc is not None and self.started_at is not None:
            stderr = b""
            if self.proc.stderr is not None:
                # The pipe may already be closed if the process was reaped
                # between the poll and this read; the exit reason is a nicety,
                # not worth failing the supervisor over.
                with contextlib.suppress(ValueError, OSError):
                    stderr = self.proc.stderr.read() or b""
            log.warning(
                "camera %s stream exited rc=%s after %.0fs: %s",
                self.cam.external_ref,
                self.proc.returncode,
                now - self.started_at,
                stderr.decode(errors="replace").strip()[-300:] or "(no output)",
            )
            self.proc = None
            self._next_attempt = now + self._backoff
            self._backoff = min(self._backoff * 2, RESTART_BACKOFF_MAX_S)
            self.restarts += 1
            return

        if now >= self._next_attempt:
            self.start()

    def stop(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None


class Simulator:
    def __init__(self, cameras: list[SimCamera], source_dir: Path, rtsp_base: str) -> None:
        self.cameras = cameras
        self.source_dir = source_dir
        self.rtsp_base = rtsp_base
        self.streams: list[StreamProcess] = []
        self._stop = threading.Event()

    def prepare(self) -> list[tuple[SimCamera, Path, float]]:
        """Build every needed profile variant, in parallel, before publishing."""
        VARIANT_DIR.mkdir(parents=True, exist_ok=True)

        # Many cameras share a (clip, profile) pair; transcode each pair once.
        by_key: dict[str, SimCamera] = {}
        for cam in self.cameras:
            by_key.setdefault(cam.variant_key, cam)

        log.info(
            "preparing %d profile variants for %d cameras",
            len(by_key), len(self.cameras),
        )
        built: dict[str, Path] = {}
        with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 4))) as pool:
            futures = {
                key: pool.submit(build_variant, cam, self.source_dir, VARIANT_DIR)
                for key, cam in by_key.items()
            }
            for key, fut in futures.items():
                path = fut.result()
                if path is not None:
                    built[key] = path

        durations = {key: probe_duration_s(path) for key, path in built.items()}

        ready: list[tuple[SimCamera, Path, float]] = []
        for cam in self.cameras:
            variant = built.get(cam.variant_key)
            if variant is None:
                log.error("camera %s has no usable variant; skipping", cam.external_ref)
                continue
            ready.append((cam, variant, durations.get(cam.variant_key, 0.0)))
        return ready

    def run(self, dry_run: bool = False) -> int:
        ready = self.prepare()
        if not ready:
            log.error("no cameras ready to publish")
            return 1

        if dry_run:
            for cam, variant, duration in ready:
                cmd = publish_command(cam, variant, duration, self.rtsp_base)
                print(f"{cam.external_ref}  {cam.path}  {' '.join(cmd)}")
            return 0

        for cam, variant, duration in ready:
            cmd = publish_command(cam, variant, duration, self.rtsp_base)
            stream = StreamProcess(cam, cmd)
            stream.start()
            self.streams.append(stream)
            time.sleep(STAGGER_S)

        log.info("publishing %d streams to %s", len(self.streams), self.rtsp_base)

        last_report = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            for stream in self.streams:
                stream.poll(now)

            if now - last_report > 30:
                alive = sum(1 for s in self.streams if s.alive)
                restarts = sum(s.restarts for s in self.streams)
                log.info(
                    "%d/%d streams live, %d restarts total",
                    alive, len(self.streams), restarts,
                )
                last_report = now

            self._stop.wait(1.0)

        log.info("shutting down %d streams", len(self.streams))
        for stream in self.streams:
            stream.stop()
        return 0

    def request_stop(self, *_: object) -> None:
        self._stop.set()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description="Publish the registry's cameras as RTSP.")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the ffmpeg commands and exit"
    )
    args = parser.parse_args()

    wait_for_db()
    cameras = load_cameras(settings.dsn)
    if not cameras:
        log.error(
            "registry has no simulated cameras; run `make seed` first "
            "(the simulator never invents cameras — invariant 4)"
        )
        return 1

    sim = Simulator(cameras, Path(settings.sim_video_dir), settings.rtsp_base)
    signal.signal(signal.SIGTERM, sim.request_stop)
    signal.signal(signal.SIGINT, sim.request_stop)
    return sim.run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
