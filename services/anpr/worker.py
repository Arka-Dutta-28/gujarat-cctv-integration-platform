"""ANPR worker: one process, many cameras.

Reads its camera list from the registry (invariant 4: nothing here knows a
stream URL), opens each one through its adapter, and runs the pipeline on the
frames. Cameras are sharded so the estate can be split across processes and
machines without any coordination: shard i of n takes every camera whose id
hashes to i. That is the whole horizontal-scaling story, and it is deliberately
this dumb, because a consistent-hash assignment needs no scheduler, no leader
and no shared state, which is what makes 80,000 cameras an arithmetic problem
rather than a distributed-systems one.

Analytics read the source stream, never the browser-facing one. The relay
publishes a re-encoded <path>-web copy for cameras a browser cannot play; that
copy is baseline H.264 at reduced quality, and reading it would degrade OCR for
no reason. The adapter's ingest_url is always the original.

How a live stream behaves is not this module's problem. Forced TCP transport,
PTS-driven timing, tolerance of decoder complaints at join, exponential
reconnect and loop-point discontinuities all live in services/anpr/capture.py,
which exists so that "do we consume the grid correctly?" is a question about one
file with its own tests rather than about a decode loop tangled up with
persistence. What is left here is what is specific to this platform: which
endpoints a camera has, what a frame means to the pipeline, and what gets
written.

The heavy imports (OpenCV, torch) are deliberately deferred into the functions
that need them, so the module can be imported, and its logic tested, without a
model runtime present.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from services.adapters import CameraRef, get_adapter
from services.adapters.credentials import apply_to_url, resolve
from services.anpr import backends, crops, escalation, ocr_boost, reid
from services.anpr.capture import Frame, LiveCapture, StreamCandidate
from services.anpr.conditions import classify
from services.anpr.escalation import EscalationBudget, EscalationPolicy
from services.anpr.metrics import MetricsCollector
from services.anpr.pipeline import AnprPipeline, FrameContext
from services.anpr.sampling import AdaptiveSampler
from services.anpr.sink import SightingSink, row_for
from services.anpr.tamper import TamperDetector
from services.anpr.tracks import TrackRegistry
from services.common.config import settings
from services.common.db import wait_for_db

log = logging.getLogger("anpr.worker")

#: Metrics rollup window. Matches the per-minute grain of the stats tables.
METRICS_WINDOW_S = 60.0

#: How long the relay supervisor is given to start a pull.
RELAY_TIMEOUT_S = 15.0

#: OpenCV threads per operation. It defaults to the core count — 20 here — and
#: applies that to `resize`, `morphologyEx`, `threshold`, the colour conversion
#: in `read()`, and everything else. This process runs one decode thread per
#: camera, so with ~14 cameras a worker and six workers on the box, the default
#: asks for up to 1,620-way parallelism on 20 cores. The symptom is that
#: measured stage latencies bear no relation to the work: `vehicle_detect`
#: showed p50 81.7 ms under load against 8.7 ms probed alone, and the tier
#: consumed 19 cores to do about 3 cores of arithmetic. The rest was threads
#: queueing for each other.
#:
#: Exactly the same mistake as the ONNX thread pool, in a second library, and
#: worth stating plainly: when parallelism already comes from one thread per
#: camera, every library underneath must be told to work serially.
OPENCV_THREADS = int(os.environ.get("ANPR_OPENCV_THREADS", "1"))


#: Which cameras this worker is allowed to own, as a comma-separated list of
#: `external_ref` patterns where `*` matches anything: `cam-2*,sentinel-*`.
#: Empty — the default — means every camera, which is the behaviour that existed
#: before this and what a single-site deployment wants.
#:
#: It exists for two reasons that turn out to be the same reason.
#:
#: **Edge placement.** A worker running at Bharuch has no business decoding
#: Rajkot. `docker-compose.edge.yml` already runs a worker against a remote
#: database; without a scope, that worker is told about the whole state and
#: sheds the difference. Sharding splits cameras *evenly*, which is the wrong
#: axis when the constraint is which streams a site can physically reach.
#:
#: **Recording a demo.** The corridor and alert cameras are nine of eighty, and
#: decoding the other seventy-one to film six of them saturates the box: at 50
#: cameras on this hardware, 425 sightings a minute arrive and three of them
#: carry a plate.
#:
#: Scoping rather than decommissioning matters. A decommissioned camera is a
#: claim about the estate that the map, the coverage analysis and the health
#: history all believe. This says only "not mine to decode", which is a fact
#: about one worker.
CAMERA_REFS = os.environ.get("ANPR_CAMERA_REFS", "").strip()


def camera_patterns(spec: str) -> list[str]:
    """Split the scope spec into SQL LIKE patterns. Empty list means no filter."""
    return [p.strip().replace("*", "%") for p in spec.split(",") if p.strip()]


def shard_of(camera_id: str, shards: int) -> int:
    """Which shard owns this camera. Stable, and needs no coordination."""
    digest = hashlib.blake2b(camera_id.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") % max(1, shards)


def fetch_cameras(
    dsn: str, shard: int, shards: int, *, refs: str | None = None
) -> list[CameraRef]:
    """Cameras this worker is responsible for. The registry is the only source.

    `endpoints` and `stream_properties` come along because the pipeline has to
    size itself per camera — the grid mixes H.264 and H.265, resolutions and
    frame rates — and because which transport is reachable is a property of the
    network this worker is running on, not something to be assumed.

    `refs` scopes the worker to a subset — see `CAMERA_REFS`. The scope is
    applied *before* sharding, so N workers still split whatever is in scope
    evenly between them rather than three of them finding they own nothing.
    """
    patterns = camera_patterns(CAMERA_REFS if refs is None else refs)
    sql = (
        "SELECT id::text, external_ref, name, adapter::text, stream_ref,"
        " credential_ref, kind::text,"
        " COALESCE(endpoints, '[]'::jsonb), COALESCE(stream_properties, '{}'::jsonb)"
        " FROM cameras"
        " WHERE status <> 'decommissioned'"
    )
    params: list = []
    if patterns:
        sql += " AND external_ref LIKE ANY(%s)"
        params.append(patterns)
    sql += " ORDER BY external_ref"

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params or None)
        rows = cur.fetchall()
    cameras = [
        CameraRef(
            id=r[0], external_ref=r[1], name=r[2], adapter=r[3],
            stream_ref=r[4], credential_ref=r[5], kind=r[6],
            endpoints=tuple(r[7] or ()), stream_properties=dict(r[8] or {}),
        )
        for r in rows
    ]
    return [c for c in cameras if shard_of(c.id, shards) == shard]


@dataclass
class FrameStats:
    """What the decoder can tell the pipeline without running a model."""

    motion: float
    mean_luma: float
    bright_fraction: float
    #: True when essentially the whole frame changed at once — a hard cut. The
    #: feeds are recordings that loop, and at the loop point the scene changes
    #: completely. Detected here because the motion figure that finds it is
    #: already being computed; the alternative is a second pass over the pixels
    #: to learn something this one already knows.
    scene_cut: bool = False


class FrameAnalyser:
    """Cheap per-frame statistics: motion, brightness, blown-out fraction.

    Runs on a heavily downscaled greyscale copy. At that size the arithmetic is
    negligible next to decoding, and it is what lets an idle camera skip the
    detector entirely — the single biggest saving in the whole pipeline.
    """

    #: Fraction of the downscaled frame that must change in one step to count
    #: as a cut rather than as traffic. A lorry filling the view is nowhere
    #: near this; a different scene entirely is well past it.
    CUT_FRACTION = float(os.environ.get("ANPR_SCENE_CUT_FRACTION", "0.55"))

    def __init__(self, scale_to: int = 160) -> None:
        self.scale_to = scale_to
        self.previous = None

    def reset(self) -> None:
        """Forget the previous frame. Called across a scene discontinuity, so
        the first frame of a new scene is not reported as total motion."""
        self.previous = None

    def analyse(self, frame) -> FrameStats:  # noqa: ANN001 - numpy array
        import cv2
        import numpy as np

        height, width = frame.shape[:2]
        if width == 0 or height == 0:
            return FrameStats(0.0, 0.0, 0.0)

        small = cv2.cvtColor(
            cv2.resize(frame, (self.scale_to, max(1, self.scale_to * height // width))),
            cv2.COLOR_BGR2GRAY,
        )

        motion = 0.0
        if self.previous is not None and self.previous.shape == small.shape:
            delta = cv2.absdiff(small, self.previous)
            # 25 levels of difference: above sensor noise, below a shadow moving.
            motion = float(np.count_nonzero(delta > 25)) / delta.size
        self.previous = small

        return FrameStats(
            motion=motion,
            mean_luma=float(small.mean()),
            # Near-saturated pixels. Headlight bloom clips completely; a bright
            # sky usually does not.
            bright_fraction=float(np.count_nonzero(small >= 250)) / small.size,
            scene_cut=motion >= self.CUT_FRACTION,
        )


#: Transports a decoder can read, best first. RTSP is what the integration
#: reference designates for inference; HLS is its documented fallback for a
#: network where 8554 is blocked. WHEP is a browser transport and is never
#: handed to FFmpeg.
INFERENCE_PROTOCOLS = ("rtsp", "hls", "http")


class CameraWorker(threading.Thread):
    """Decodes one camera and runs the pipeline over its frames.

    The decode loop itself lives in `services/anpr/capture.py`, which owns
    every rule about how a live RTP grid behaves — TCP transport, PTS timing,
    join tolerance, backoff, loop-point discontinuities. What is left here is
    the part that is about *this platform*: which endpoints exist for this
    camera, what a frame means to the pipeline, and what to persist.
    """

    def __init__(
        self,
        camera: CameraRef,
        sink: SightingSink,
        stop: threading.Event,
        escalation_budget: EscalationBudget | None = None,
        boosts: ocr_boost.BoostCache | None = None,
        readers: ocr_boost.SharedReaders | None = None,
        connect: Any = None,
    ) -> None:
        super().__init__(name=f"anpr-{camera.path}", daemon=True)
        self.camera = camera
        self.sink = sink
        self.stop = stop
        self.metrics = MetricsCollector(camera.id, window_s=METRICS_WINDOW_S)
        self.pipeline = AnprPipeline(
            camera_id=camera.id,
            tracker=_build_tracker(),
            locator=_build_locator(),
            ocr=_build_ocr(),
            sampler=AdaptiveSampler(),
            registry=TrackRegistry(camera.id),
            metrics=self.metrics,
        )
        self.decoded_since_rollup = 0
        self.rollup_started = time.monotonic()
        #: One per camera: the established scene is a property of this camera.
        self.tamper = TamperDetector()
        self.analyser = FrameAnalyser()
        #: Watches this camera's own read rate and swaps in the heavy models if
        #: the light ones are measured failing on it. The budget is shared
        #: across every camera in the process, so escalations cannot all happen
        #: at once and starve the decoders.
        self.escalation = EscalationPolicy(camera=camera.path, budget=escalation_budget)
        #: Maps the capture's media timeline onto wall-clock time, so a
        #: sighting is stamped with when the vehicle was actually last seen
        #: rather than with when the harvest loop got round to it.
        self.wall_offset = 0.0
        #: Set once a direct open has proved unreliable for this camera.
        self.prefer_relay = False
        #: Operator-requested reader for this camera (`/api/ocr-boosts`).
        self.boosts, self.readers, self.connect = boosts, readers, connect
        self.boost_id: int | None = None
        self.boost_reader: Any = None
        self.pre_boost_ocr: Any = None

    # --- endpoints ------------------------------------------------------

    def candidates(self) -> list[StreamCandidate]:
        """Every way to reach this camera, best first — all from the registry.

        The catalogue publishes three transports per camera and the registry
        stores all of them, so a network where RTSP is blocked degrades to HLS
        instead of to nothing. Cameras onboarded before the catalogue existed —
        the simulated farm, file clips, a bulk CSV import — have no endpoint
        list, and fall back to the adapter's own ingest URL. Neither path
        constructs a URL from a pattern.
        """
        ladder: list[StreamCandidate] = []
        seen: set[str] = set()
        # A camera with a login gets it on its RTSP endpoints here, not only on
        # the adapter fallback at the end. The government grid answers 401 to an
        # RTSP request without one (14 Sep 2026), so otherwise every connect and
        # every reconnect spent a refused request, and a refused HLS one, on
        # someone else's server before reaching the address that works.
        credential = resolve(self.camera.credential_ref)

        for protocol, url in self.camera.endpoint_urls(INFERENCE_PROTOCOLS):
            if credential is not None and protocol == "rtsp":
                url = apply_to_url(url, credential)
            if url not in seen:
                seen.add(url)
                ladder.append(StreamCandidate(url=url, protocol=protocol))

        # The adapter's ingest URL: always the source stream, never the relay's
        # reduced-quality `-web` re-encode, which would degrade OCR for nothing.
        try:
            fallback = get_adapter(self.camera.adapter).ingest_url(self.camera)
        except LookupError:
            log.exception("camera %s: no adapter %r", self.camera.path, self.camera.adapter)
            fallback = None
        if fallback and fallback not in seen:
            ladder.append(
                StreamCandidate(url=fallback, protocol=self.camera.adapter)
            )

        if self.prefer_relay:
            relayed = self._relay_candidate()
            if relayed is not None:
                ladder.insert(0, relayed)
        return ladder

    def _relay_candidate(self) -> StreamCandidate | None:
        """Ask the relay to pull this camera, and read from the media server.

        For a source we cannot open ourselves, the relay is already the
        component whose job is pulling awkward sources. It republishes with
        `-c copy`, so this is the original stream and not the reduced-quality
        `-web` re-encode the browser gets.

        It is also less work upstream, not more: one pull feeds both the
        operator watching and the analytics reading, rather than each opening
        its own connection to a camera that is often on a constrained link.
        """
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed internal URL
                urllib.request.Request(
                    f"{settings.relay_url}/relay/{self.camera.id}", method="POST"
                ),
                timeout=RELAY_TIMEOUT_S,
            ) as resp:
                state = json.load(resp)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            log.warning("camera %s: relay unavailable (%s)", self.camera.path, exc)
            return None

        path = state.get("path")
        if not path:
            return None
        self.metrics.count("via_relay")
        return StreamCandidate(
            url=f"{settings.rtsp_base}/{path}", protocol="rtsp", managed=True
        )

    # --- decode loop ----------------------------------------------------

    def run(self) -> None:
        ladder = self.candidates()
        if not ladder:
            log.error("camera %s: no endpoint in the registry", self.camera.path)
            return

        log.info(
            "camera %s: %s (codec %s, %s)",
            self.camera.path,
            " -> ".join(c.protocol for c in ladder),
            self.camera.stream_properties.get("codec", "unreported"),
            self.camera.stream_properties.get("resolution")
            or f"{self.camera.stream_properties.get('width', '?')}"
            f"x{self.camera.stream_properties.get('height', '?')}",
        )

        capture = LiveCapture(
            ladder,
            stop=self.stop,
            label=f"camera {self.camera.path}",
            on_stats=self._record_session,
            # Only consulted when nothing in the ladder opened, which is the
            # government feeds' failure mode and exactly when the relay earns
            # its keep.
            on_exhausted=self._relay_candidate,
        )

        try:
            for frame in capture.frames():
                try:
                    self._on_frame(frame)
                except Exception:  # noqa: BLE001 - one bad frame must not kill a camera
                    log.exception("camera %s: frame failed", self.camera.path)
        finally:
            self._persist(self.pipeline.flush())
            self.sink.flush()
            self.escalation.close()

    def _on_frame(self, frame: Frame) -> None:
        """Everything that happens to one decoded frame."""
        self.decoded_since_rollup += 1
        self.wall_offset = frame.wall_ts - frame.monotonic_s

        with self.metrics.stage("frame_stats"):
            stats = self.analyser.analyse(frame.pixels)

        # A loop point, a gateway restart, or a hard cut in the recording. Any
        # of them means the scene the long-lived state describes no longer
        # exists, so it is retired rather than carried forward.
        if frame.discontinuity or stats.scene_cut:
            self._on_discontinuity(frame, content_cut=stats.scene_cut)
            stats = self.analyser.analyse(frame.pixels)

        self._apply_boost()
        completed = self.pipeline.process(
            frame.pixels,
            FrameContext(
                # Media time, not arrival time. During the buffered replay at
                # join, arrival time compresses seconds of video into
                # milliseconds and every dwell and speed derived from it is
                # wrong — which is precisely the failure the reference warns
                # about, and precisely what the journey plausibility check
                # would then report as a cloned plate.
                now=frame.monotonic_s,
                motion=stats.motion,
                condition=classify(stats.mean_luma, stats.bright_fraction),
            ),
        )
        self._persist(completed)
        if completed:
            self._maybe_escalate(completed)
        # Tamper is a question about the *camera*, not about the vehicles in
        # front of it, so it runs on frames the pipeline has already decoded
        # rather than on a separate capture. Sampled every few seconds, it
        # costs a few statistics over pixels already paid for.
        self._check_tamper(frame.pixels, frame.monotonic_s)
        # Housekeeping runs on the *local* clock, not on media time. Batch
        # age and the metrics window are facts about this process, and a
        # stalled stream must not stop it flushing what it already has.
        self._maybe_roll_up(time.monotonic())

    def _on_discontinuity(self, frame: Frame, *, content_cut: bool) -> None:
        """Retire everything that described the scene that just ended.

        Each feed is a continuous recording that loops, and the reference is explicit
        that long-lived state must recover from the cut rather than assume infinite
        continuity. Concretely:

          - open tracks are completed and written, not discarded. A vehicle in view at
            the loop point was a real vehicle and its reads are real reads; dropping
            them would lose sightings, which is the one thing this platform must never
            do;
          - the tracker's id assignment is cleared, so a car in the new scene does not
            inherit the identity of a car in the old one;
          - the tamper reference is cleared, because the established view has
            legitimately changed and comparing against the old one reports a moved
            camera every single loop;
          - the motion baseline is cleared, so the first frame of the new scene is not
            read as the whole frame moving.
        """
        self.metrics.count("scene_discontinuities")
        log.info(
            "camera %s: scene discontinuity at %.1fs (%s) — retiring scene state",
            self.camera.path, frame.monotonic_s,
            "content cut" if content_cut else "stream timeline",
        )
        self._persist(self.pipeline.flush())
        reset = getattr(self.pipeline.tracker, "reset", None)
        if callable(reset):
            reset()
        self.tamper = TamperDetector()
        self.analyser.reset()

    def _apply_boost(self) -> None:
        """Read with the operator's chosen reader while this camera is boosted.

        Checked every frame against a cache refreshed every few seconds, so it
        costs a dictionary lookup. Loading the model the first time blocks this
        camera's thread only, as escalation does. The outcome is written back to
        the boost row either way.
        """
        if self.boosts is None:
            return
        wanted = self.boosts.wanted(self.camera.id)
        wanted_id = wanted[0] if wanted else None
        if wanted_id != self.boost_id:
            if self.boost_reader is not None:
                self.pipeline.ocr = self.pre_boost_ocr
                self.boost_reader = self.pre_boost_ocr = None
                log.info("camera %s: OCR boost ended; back to the default reader",
                         self.camera.path)
            self.boost_id = wanted_id
            if wanted is not None and self.readers is not None:
                reader, error = self.readers.get(wanted[1])
                if reader is not None:
                    self.pre_boost_ocr, self.boost_reader = self.pipeline.ocr, reader
                    log.info("camera %s: OCR boost %s: reading with %s",
                             self.camera.path, wanted_id, wanted[1])
                ocr_boost.report(self.connect, wanted_id, error)
        if self.boost_reader is not None and self.pipeline.ocr is not self.boost_reader:
            # First frame of a boost, or escalation swapped the reader underneath
            # one. The operator's request wins until it ends.
            self.pre_boost_ocr = self.pipeline.ocr
            self.pipeline.ocr = self.boost_reader

    def _maybe_escalate(self, completed: list) -> None:  # noqa: ANN001
        """Swap a stage for its heavy model if this camera needs one.

        The policy decides *whether* and *which*; this does the swapping, and it
        does it by name — `setattr` on the pipeline attribute the rung names —
        so a fourth analytics stage is a new rung in the ladder and no new
        branch here.
        """
        rung = self.escalation.observe(completed)
        if rung is None:
            return

        builders = {
            "ocr": backends.build_ocr,
            "locator": backends.build_locator,
            "tracker": backends.build_tracker,
        }
        try:
            if rung.stage == "__reset__":
                # Everything back to the process default. The camera reads
                # nothing on any tier and there is no sense paying heavy prices
                # for that; the warning the policy logged is the useful output.
                for stage, build in builders.items():
                    setattr(self.pipeline, stage, build())
                self.metrics.count("escalations_abandoned")
                return

            if rung.stage == "ocr" and _is_learned_reader(self.pipeline.ocr):
                # The "heavy" OCR is the hub plate model, which reads 0 of the
                # 42 hand-checked government plates; docTR reads 24 and
                # PaddleOCR-VL 27. Found 14 Sep 2026: escalation swapped a
                # PaddleOCR-VL camera down to it. Skipping the rung lets the
                # ladder move on to the locator, which is the useful next step.
                log.info("camera %s: OCR rung skipped — %s is already stronger than the heavy "
                         "OCR", self.camera.path, type(self.pipeline.ocr).__name__)
                self.metrics.count("escalations_skipped")
                return

            build = builders.get(rung.stage)
            if build is None:
                log.error("camera %s: no builder for stage %r", self.camera.path, rung.stage)
                return
            # Built here, in this camera's own decode thread, so a model that
            # has to be loaded blocks one camera rather than the worker.
            setattr(self.pipeline, rung.stage, build(rung.tier))
            self.metrics.count("escalations")
        except Exception:  # noqa: BLE001 - a heavy model that will not load is
            # not a reason to lose the camera. It keeps running on whatever it
            # already had, which is the light path that was at least decoding.
            log.exception(
                "camera %s: could not load the %s model for %s; staying on the "
                "light path", self.camera.path, rung.tier, rung.stage,
            )
            self.metrics.count("escalations_failed")

    def _record_session(self, stats: Any) -> None:
        """Fold one capture session's measurements into the metrics."""
        self.metrics.count("capture_sessions")
        self.metrics.count("failed_reads", stats.failed_reads)
        self.metrics.count("scene_discontinuities_pts", stats.discontinuities)
        if stats.clock == "arrival":
            # Visible in the numbers rather than only in a log line: a camera
            # whose timing came from arrival rather than PTS produces weaker
            # dwell and speed evidence, and an operator reading an accuracy
            # figure deserves to know which cameras those were.
            self.metrics.count("sessions_without_pts")

    def _check_tamper(self, frame: Any, now: float) -> None:
        """Record a suspected interference event, at most once per condition."""
        verdict = self.tamper.observe(frame, now)
        if verdict is None:
            return
        log.warning(
            "camera %s: possible tampering (%s) — %s",
            self.camera.path, verdict.kind, verdict.detail,
        )
        self.sink.record_tamper(self.camera.id, verdict)

    def _persist(self, completed: list) -> None:  # noqa: ANN001
        if not completed:
            return
        self.metrics.count("vehicles_tracked", len(completed))
        tracks = [c.track for c in completed]
        vectors = reid.embed_many([t.best_crop for t in tracks])
        for c, track, vector in zip(completed, tracks, vectors, strict=True):
            # An unread vehicle linked by appearance needs a picture of the frame
            # the vector came from, or nobody can check the link.
            if vector is not None and (c.result is None or not track.crop_jpeg):
                track.crop_jpeg = crops.encode(track.best_crop)
            track.appearance, track.best_crop = vector, None
        rows = [r for r in (row_for(c, self._seen_at(c)) for c in completed) if r is not None]
        self.metrics.count("sightings_written", len(rows))
        self.sink.add(rows)

    def _seen_at(self, completed: Any) -> datetime:
        """When this vehicle was last seen, in wall-clock time.

        Ingest-side time, deliberately: the burnt-in clocks on these feeds are
        wrong by weeks and disagree with each other, so a journey reconstructed
        from them would be nonsense.

        But *which* ingest instant matters, and it is not `now()`. A track is
        harvested a few seconds after the vehicle left the frame, and under load
        the harvest can lag further, so `now()` systematically stamps every
        sighting late by a variable amount — which is exactly the error a
        cross-camera journey is most sensitive to. The capture layer anchors
        media time to wall time, so the vehicle's own last-seen instant is
        available and is what gets stored.
        """
        last_seen = getattr(getattr(completed, "track", None), "last_seen", None)
        if last_seen is None or not self.wall_offset:
            return datetime.now(UTC)
        return datetime.fromtimestamp(self.wall_offset + last_seen, UTC)

    def _maybe_roll_up(self, now: float) -> None:
        if self.sink.due(now):
            self.sink.flush()
        # A camera that fell back to motion proposals must say so in the
        # numbers; a fallback nobody can see becomes an accuracy figure nobody
        # can explain.
        if getattr(self.pipeline.tracker, "fell_back", False):
            self.metrics.count("motion_fallback_windows")

        if not self.metrics.due(now):
            return
        elapsed = now - self.rollup_started
        self.sink.write_metrics(
            self.metrics, decode_fps=self.decoded_since_rollup / elapsed if elapsed else None
        )
        self.metrics.reset(now)
        self.decoded_since_rollup = 0
        self.rollup_started = now


# --- model construction ------------------------------------------------
# The tiers themselves live in `services/anpr/backends`, because three
# different callers have to agree on which implementation is "light" and which
# is "heavy": this worker choosing a process default, the escalation policy
# swapping one camera up, and the accuracy harness forcing a tier to compare
# them. One definition, three users.

VEHICLE_BACKEND = backends.VEHICLE_BACKEND
PLATE_BACKEND = backends.PLATE_BACKEND
OCR_BACKEND = backends.OCR_BACKEND

_build_tracker = backends.build_tracker
_build_locator = backends.build_locator
_build_ocr = backends.build_ocr


def _is_learned_reader(ocr: Any) -> bool:
    """docTR or PaddleOCR-VL: readers the hub model must never replace."""
    from services.anpr.backends.learned_ocr import _Lazy

    return isinstance(ocr, _Lazy)
FallbackOcr = backends.FallbackOcr


#: Namespace for the advisory locks below, so a shard claim cannot collide with
#: any other advisory lock the database is asked for.
SHARD_LOCK_NAMESPACE = 0x414E5052  # "ANPR"


def claim_shard(dsn: str, shards: int) -> tuple[int, object] | None:
    """Take the lowest free shard, and hold it for the life of the process.

    Replicas need distinct shard numbers or they duplicate each other's work
    while leaving cameras unprocessed. The obvious trick — read the ordinal off
    the container name — does not survive contact with Compose, which sets the
    hostname to a random hex id; six replicas took shards 0, 0, 3, 3, 3, 4 and
    the failure was silent.

    A Postgres advisory lock is the right primitive. It is held by the *session*
    and released the moment the connection drops, so a worker that crashes gives
    its shard straight back and the replacement picks it up with no timeout, no
    heartbeat and no reaper. The operator still runs one command and scales; the
    coordination is real but invisible.

    Returns the shard and the connection holding it — which the caller must keep
    open, since closing it releases the claim.
    """
    conn = psycopg.connect(dsn, autocommit=True)
    for shard in range(max(1, shards)):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(%s, %s)", (SHARD_LOCK_NAMESPACE, shard)
            )
            row = cur.fetchone()
        if row and row[0]:
            return shard, conn

    # More replicas than shards. Exiting rather than idling: a container that
    # silently does nothing is worse than one that restarts and says why.
    conn.close()
    return None


#: Advisory-lock id, inside the namespace above, serialising the model warm
#: across replicas. Far from any shard number, which are small integers.
MODEL_WARM_LOCK = 0x7FFF


def prewarm_models(dsn: str) -> None:
    """Load every model handle before the first camera thread starts.

    Two problems, one fix.

    The loaders hold a process-wide lock while they download, so reaching them
    lazily from the first crop freezes every camera thread in the worker, not
    just the one that asked. Doing it here means no decode loop is ever waiting
    on a socket.

    And the replicas share one model cache volume, so only the first of them
    should be downloading at all. A blocking advisory lock — the same mechanism
    that hands out shards, on infrastructure already present — makes the rest
    wait and then find the weights on disk. Six copies of a 7.4 MB download over
    a ~50 kB/s link is not a rounding error here; it is minutes of dead estate.

    Failure to warm is not fatal: both stages have a fallback that needs no
    download, which is the whole reason they exist.
    """
    started = time.monotonic()
    conn = None
    try:
        conn = psycopg.connect(dsn, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s, %s)", (SHARD_LOCK_NAMESPACE, MODEL_WARM_LOCK))

        from services.anpr.backends.plates import prewarm_detector, prewarm_reader

        # The heavy models are warmed even when this run defaults to the light
        # ones, because escalation can reach for them at any moment. Paying a
        # cold download inside a decode thread is exactly the stall this
        # function exists to prevent, and it would land on the camera that was
        # already reading badly enough to need help.
        detector = prewarm_detector()
        reader = prewarm_reader()
        uses_hub = OCR_BACKEND in {"hub", "hub-first"}
        if VEHICLE_BACKEND != "motion":
            from services.anpr.backends.yolo import _shared_model

            _shared_model()
        log.info(
            "models warm in %.1fs — vehicles: %s, plate detector: %s, plate OCR: %s "
            "(heavy tier %s, held in reserve for cameras the light path cannot read)",
            time.monotonic() - started,
            VEHICLE_BACKEND,
            "learned" if PLATE_BACKEND in {"learned", "hub"} else "classical",
            "learned (hub)" if uses_hub else type(backends.build_ocr()).__name__,
            "available" if (detector and reader) else "unavailable",
        )
    except Exception as exc:  # noqa: BLE001 - a cold model must not stop the estate
        log.warning("model warm-up incomplete (%s); fallbacks will be used", exc)
    finally:
        if conn is not None:
            conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="ANPR worker.")
    parser.add_argument("--shard", type=int, default=None)
    parser.add_argument("--shards", type=int, default=None)
    parser.add_argument("--limit", type=int, default=0, help="Cap cameras, for measurement runs.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    )
    wait_for_db()

    import cv2

    cv2.setNumThreads(OPENCV_THREADS)
    log.info("opencv threads per operation: %d", cv2.getNumThreads())

    shards = args.shards if args.shards is not None else int(os.environ.get("ANPR_SHARDS", "1"))

    claim = None
    if args.shard is not None:
        shard = args.shard % max(1, shards)
    else:
        claim = claim_shard(settings.dsn, shards)
        if claim is None:
            log.error(
                "no free shard: %d replicas for %d shards. Raise ANPR_SHARDS or "
                "reduce the replica count.", shards + 1, shards,
            )
            return 1
        shard, _holder = claim

    cameras = fetch_cameras(settings.dsn, shard, shards)
    if args.limit:
        cameras = cameras[: args.limit]
    log.info("shard %d of %d owns %d cameras", shard, shards, len(cameras))

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    prewarm_models(settings.dsn)

    # Live alerting (M5) rides the sighting write, in the same transaction. It
    # is a *separate code path* from the retrospective trace (invariant 2) —
    # they share the sightings table and no code — but it must see a sighting the
    # moment it is written, and this is that moment.
    from services.alerting.watchlist import REFRESH_S, WatchlistCache
    from services.alerting.writer import AlertWriter

    def connect() -> Any:
        # `dict_row`, matching the API's pool. Without it this connection yields
        # tuples while every module that queries through it — the sink's
        # RETURNING, the watchlist loader — was written against the API's
        # behaviour, and both failed with `tuple indices must be integers`. One
        # connection factory, one row shape, rather than each call site
        # remembering which kind of connection it is on.
        from psycopg.rows import dict_row

        return psycopg.connect(settings.dsn, row_factory=dict_row)

    alerts = AlertWriter(watchlist=WatchlistCache(connect=connect))
    sink = SightingSink(connect=connect, alerts=alerts, link=True)
    log.info(
        "live alerting armed: %d active watchlist entries, refreshed every %.0fs",
        len(alerts.watchlist.entries()), REFRESH_S,
    )
    budget = EscalationBudget()
    log.info(
        "escalation armed: a camera reading under %.0f%% of its vehicles over %d "
        "tracks moves to the heavy models, at most %d cameras at a time",
        escalation.READ_RATE_FLOOR * 100, escalation.MIN_SAMPLE, budget.size,
    )
    boosts = ocr_boost.BoostCache(connect, [c.id for c in cameras])
    readers = ocr_boost.SharedReaders(backends.build_ocr)
    log.info("OCR boost armed: operator requests polled every %.0fs", ocr_boost.REFRESH_S)
    workers = [
        CameraWorker(c, sink, stop, escalation_budget=budget, boosts=boosts, readers=readers,
                     connect=connect)
        for c in cameras
    ]
    for w in workers:
        w.start()

    while not stop.is_set():
        stop.wait(1.0)
        if sink.due():
            sink.flush()

    log.info("stopping")
    for w in workers:
        w.join(timeout=10)
    sink.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
