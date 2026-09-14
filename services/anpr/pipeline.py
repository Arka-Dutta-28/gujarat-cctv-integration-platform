"""The ANPR pipeline for one camera.

Order matters and is not arbitrary:

    decode, sample, detect and track vehicles, locate the plate inside the
    vehicle box, OCR, per-track vote, persist every read

Two of those steps are there because of what the real feeds turned out to be.

Plate detection is confined to a vehicle box. Every government feed has burnt-in
text: a timestamp band, a camera name, REC, a site label, some of it
high-contrast white on dark at a similar size to a plate. Run OCR on the whole
frame and CSITMS-32 and 14-06-2026 become plate candidates. They would then be
persisted, because the invariant says format failures are kept and flagged, so
the noise would accumulate in sightings rather than being quietly dropped.
Confining the search is the cheap fix.

Every read is persisted, not just watchlist hits. The evaluator's plate arrives
after the vehicle has passed. This is the single most important rule in the
project, and the pipeline has no filter anywhere that could break it: the only
thing that stops a track becoming a sighting is producing no readable characters
at all.

Holds no model, no socket and no database handle, since everything comes in
through the constructor, so the ordering, the sampling and the voting can be
tested on stubs. The worker wires the real ones in.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

from services.anpr.metrics import MetricsCollector
from services.anpr.models import (
    VEHICLE_CLASSES,
    Box,
    PlateLocator,
    PlateOcr,
    VehicleTracker,
)
from services.anpr.overlay import StaticTextFilter
from services.anpr.sampling import AdaptiveSampler
from services.anpr.tracks import CompletedTrack, TrackRegistry
from services.anpr.vote import PlateRead

log = logging.getLogger("anpr.pipeline")

__all__ = [
    "AnprPipeline",
    "FrameContext",
    "MIN_VEHICLE_AREA_FRACTION",
    "MIN_VEHICLE_AREA_PX",
    "PLATE_CROP_PADDING_PX",
    "min_vehicle_area",
]

#: Below this share of the frame, a vehicle is too far away for its plate to
#: survive OCR, and attempting it costs a detector pass to produce noise.
#:
#: A *fraction*, not a pixel count, and that is the point. The threshold used
#: to be 2,000 px "tuned for 1080p", which is the mistake the integration
#: reference names directly: the grid mixes resolutions, so one pixel count
#: means different things on different cameras. On a 640x480 feed 2,000 px is
#: 0.65% of the frame — a vehicle well worth reading — and on a 4K feed it is
#: 0.024%, a smudge the OCR cannot use. A fixed threshold therefore skips
#: readable vehicles on the small cameras and wastes OCR on unreadable ones on
#: the large cameras, simultaneously.
#:
#: 0.1% of the frame reproduces the old behaviour at 1080p (2,073 px) and
#: scales correctly either side of it.
MIN_VEHICLE_AREA_FRACTION = float(os.environ.get("ANPR_MIN_VEHICLE_FRACTION", "0.001"))

#: Floor in absolute pixels, for the genuinely tiny frames some legacy analog
#: encoders still produce. Below about this, no OCR engine reads a plate at any
#: resolution, so the proportional rule is allowed to go no lower.
MIN_VEHICLE_AREA_PX = int(os.environ.get("ANPR_MIN_VEHICLE_AREA_PX", "600"))


def min_vehicle_area(width: int, height: int) -> int:
    """Smallest vehicle box worth reading on a frame of this size."""
    return max(MIN_VEHICLE_AREA_PX, int(width * height * MIN_VEHICLE_AREA_FRACTION))


#: Plate detectors cut tight; OCR reads better with a little margin around the
#: characters.
PLATE_CROP_PADDING_PX = 4

#: How many plate reads may be in flight at once in this process.
#:
#: OCR is the only stage that does not degrade gracefully. The lean backend
#: shells out to tesseract per crop, so with fifty cameras of threads all
#: reading at once its p50 went from 86 ms to 23 seconds — and, far worse, the
#: threads blocked in OCR stopped decoding, dropping mean decode from 17.8 fps
#: to 1.25.
#:
#: So OCR is bounded and *shed* rather than queued. A frame that cannot get a
#: slot is not read, and the vehicle is still tracked and still produces its
#: sighting from the reads that did land. That is the right way round: skipping
#: one read weakens one vote, whereas a starved decoder loses whole vehicles it
#: never saw.
#: Two, and raising it was tried and measured as worse. At 77 cameras, going
#: from 2 to 4 per worker moved the shed rate from 84.5% to 96.9% and OCR p50
#: from 1.7 s to 15.7 s, cutting reads roughly fivefold. That is queueing theory
#: rather than a surprise: widening a bound on a saturated resource does not
#: create CPU, it spreads the same CPU over more concurrent work so each unit
#: takes longer, holds its slot longer, and sheds more of what arrives behind it.
#:
#: The bound is there to protect the decoder, and the honest fix for a shed rate
#: this high is fewer cameras per box — the sizing figure in PROGRESS is ~50-57
#: for this 20-core host, not the 77 it was carrying when this was measured.
OCR_CONCURRENCY = int(os.environ.get("ANPR_OCR_CONCURRENCY", "2"))
_ocr_slots = threading.BoundedSemaphore(OCR_CONCURRENCY)


@dataclass
class FrameContext:
    """Everything about one decoded frame that is not the pixels."""

    now: float
    #: Fraction of the frame that changed since the last one, 0-1.
    motion: float = 0.0
    #: Scene condition for per-condition accuracy reporting.
    condition: str | None = None
    #: Seconds into the upstream 12-hour playback slot, for reproducibility.
    slot_offset: float | None = None


class AnprPipeline:
    def __init__(
        self,
        camera_id: str,
        tracker: VehicleTracker,
        locator: PlateLocator,
        ocr: PlateOcr,
        *,
        sampler: AdaptiveSampler | None = None,
        registry: TrackRegistry | None = None,
        metrics: MetricsCollector | None = None,
        min_vehicle_area_px: int | None = None,
        keep_crops: bool = True,
        describe_vehicles: bool | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.tracker = tracker
        self.locator = locator
        self.ocr = ocr
        self.sampler = sampler or AdaptiveSampler()
        self.registry = registry or TrackRegistry(camera_id)
        self.metrics = metrics or MetricsCollector(camera_id)
        #: An explicit override, for a camera with a known unusual view. Left
        #: None — the normal case — the threshold is derived from each frame's
        #: own size, because the grid is not uniform.
        self.min_vehicle_area_px = min_vehicle_area_px
        #: Off in the probe harness and in tests, where a JPEG per read would
        #: measure image encoding rather than the pipeline.
        self.keep_crops = keep_crops
        #: Colour and class for every tracked vehicle, read or not. On by
        #: default because the cameras it matters for are the majority of the
        #: estate, and switchable because it is the one stage whose output
        #: volume does not depend on how many plates were read — an operator
        #: sizing a deployment needs to be able to turn it off.
        self.describe_vehicles = (
            os.environ.get("ANPR_DESCRIBE_VEHICLES", "on").strip().lower()
            in {"1", "true", "yes", "on"}
            if describe_vehicles is None
            else describe_vehicles
        )
        self.warmed_up = False
        #: Learns this camera's burnt-in furniture. One per camera, because the
        #: overlay is a property of the camera, not of the estate.
        self.overlay = StaticTextFilter()

    # --- per frame ---

    def process(self, frame: Any, ctx: FrameContext) -> list[CompletedTrack]:
        """Handle one decoded frame; return any tracks that finished on it.

        Returning finished tracks rather than writing them keeps persistence out
        of here: the caller decides what a sighting costs to store.
        """
        self.metrics.count("frames_decoded")

        if not self.sampler.should_analyse(
            ctx.now, motion=ctx.motion, active_tracks=self.registry.active
        ):
            # Still harvest: a vehicle that has left the frame should have its
            # sighting written promptly, not on whenever we next look.
            return self.registry.harvest(ctx.now)

        self.metrics.count("frames_analysed")

        # The first inference on a camera pays for CUDA context creation and
        # cuDNN autotuning — 252 seconds on the first run measured here. That is
        # a real cold-start cost and worth reporting, but averaging it into
        # `vehicle_detect` would make the p95 meaningless, so it is timed as its
        # own stage and happens exactly once.
        stage = "vehicle_detect" if self.warmed_up else "model_warmup"
        with self.metrics.stage(stage):
            vehicles = self.tracker.track(frame)
        self.warmed_up = True

        height, width = self._frame_size(frame)
        # Recomputed per frame rather than per camera: a stream can change
        # resolution mid-session, and after a loop point or a gateway restart
        # it legitimately does.
        min_area = self.min_vehicle_area_px or min_vehicle_area(width, height)

        for vehicle in vehicles:
            if vehicle.track_id is None or vehicle.label not in VEHICLE_CLASSES:
                continue
            box = vehicle.box.clamped(width, height)
            existing = self.registry.tracks.get(vehicle.track_id)
            # Described before any of the OCR branching, and deliberately so.
            # Every `continue` below is a vehicle this pipeline has decided not
            # to read a plate from — too small, or already settled — and on the
            # government estate that is almost all of them. Those are precisely
            # the vehicles for which a colour is the only description that will
            # ever exist, so the description cannot sit behind a successful
            # read the way the evidence crop does.
            appearance = self._appearance(frame, box, existing, vehicle.label)

            if box.area < min_area:
                # Tracked, so it counts towards "vehicles seen" — just too small
                # to read. Silently skipping it would flatter the read rate.
                self.registry.observe(
                    vehicle.track_id, ctx.now, vehicle_class=vehicle.label,
                    bbox=box.as_tuple(), condition=ctx.condition,
                    slot_offset=ctx.slot_offset, attributes=appearance,
                )
                continue

            # Skip OCR entirely on a track whose vote has already settled. The
            # vehicle is still tracked and still counted; we simply stop paying
            # to re-read a plate we are already sure of.
            if existing is not None and not existing.needs_more_reads():
                self.registry.observe(
                    vehicle.track_id, ctx.now, vehicle_class=vehicle.label,
                    bbox=box.as_tuple(), condition=ctx.condition,
                    slot_offset=ctx.slot_offset, attributes=appearance,
                )
                self.metrics.count("ocr_skipped_settled")
                continue

            read, plate_box = self._read_plate(
                frame, box, width, height, track_id=str(vehicle.track_id), now=ctx.now
            )
            self.registry.observe(
                vehicle.track_id,
                ctx.now,
                read=read,
                vehicle_class=vehicle.label,
                bbox=box.as_tuple(),
                plate_bbox=plate_box.as_tuple() if plate_box else None,
                condition=ctx.condition,
                slot_offset=ctx.slot_offset,
                crop_jpeg=self._evidence_crop(frame, box, plate_box, read, existing),
                attributes=appearance,
            )
            if read is not None:
                self.metrics.count("plate_reads")

        return self.registry.harvest(ctx.now)

    def _evidence_crop(
        self, frame: Any, box: Box, plate_box: Box | None, read: Any, existing: Any
    ) -> bytes | None:
        """JPEG of this vehicle, but only when it is worth encoding.

        Encoding costs a few milliseconds, which is nothing once — and real
        money at 20 fps across 50 cameras. It is spent only when this read is the
        best the track has produced, because that is the only frame that will
        end up on the sighting. In practice that is a handful of times per
        vehicle rather than once per frame.
        """
        if read is None or not self.keep_crops:
            return None
        if existing is not None and read.confidence < existing.best_read_confidence:
            return None
        from services.anpr import crops

        vehicle_crop = frame[box.y1 : box.y2, box.x1 : box.x2]
        # The plate box arrives in frame coordinates; the crop is its own origin.
        relative = plate_box.shifted(-box.x1, -box.y1) if plate_box else None
        return crops.encode(vehicle_crop, relative)

    def _appearance(self, frame: Any, box: Box, existing: Any, label: str) -> Any:
        """Everything this pipeline can say about how a vehicle looks.

        Both appearance measurements, the words (attributes.describe) and the vector
        (reid.embed), are computed here, on one frame, under one budget rule. They are
        the same kind of measurement and they answer to the same physics: both are
        proportions of visible bodywork, and both are most reliable when the most
        bodywork is visible.

        Budgeted on box growth. A vehicle approaching a camera produces a sequence of
        boxes that grows and then shrinks, so this fires perhaps a dozen times across a
        track and never again once the vehicle starts leaving. That is both the cheap
        schedule and the correct one.

        Not gated on a plate read, and this is the fix that matters. Until 6 Sep 2026
        the embedding was computed only on the frame that read best, which meant a track
        that never read a plate carried no embedding at all. The cameras that most need
        to recognise a vehicle without its plate were therefore the only cameras that
        could not. On the 30 government feeds, where a read essentially never comes,
        re-ID was switched off by an accident of where the call sat.

        Choosing the largest box rather than the best-reading one is a second,
        deliberate change. The best-reading frame is chosen for plate geometry, square
        on and sharp across four characters, which is not the same thing as the best
        view of the bodywork. The evidence crop still keeps that frame, because the
        plate is what an officer judges. Appearance now comes from one frame
        consistently, so a sighting's colour and its embedding describe the same view of
        the same vehicle.
        """
        if not self.describe_vehicles:
            return None
        if existing is not None and box.area <= existing.best_box_area:
            return None
        from services.anpr import attributes, reid

        crop = frame[box.y1 : box.y2, box.x1 : box.x2]
        # Carried on one object rather than returned as a pair: they are one
        # measurement of one frame, and keeping them apart is how they drifted
        # onto two different frames in the first place.
        return attributes.describe(crop, label).with_embedding(
            reid.embed(crop), getattr(crop, "copy", lambda: None)()
        )

    # --- one vehicle ---

    def _read_plate(
        self,
        frame: Any,
        vehicle_box: Box,
        width: int,
        height: int,
        track_id: str = "",
        now: float = 0.0,
    ) -> tuple[PlateRead | None, Box | None]:
        """Locate and read a plate within one vehicle box.

        The search never leaves the vehicle box: that is what keeps burnt-in
        overlays out of `sightings`.
        """
        crop = frame[vehicle_box.y1 : vehicle_box.y2, vehicle_box.x1 : vehicle_box.x2]

        with self.metrics.stage("plate_detect"):
            plates = self.locator.locate(crop)
        if not plates:
            return None, None

        # The most confident plate in the box. A vehicle showing two plates is
        # either two vehicles overlapping or a reflection; taking the best one
        # and letting the vote sort it out beats guessing here.
        best = max(plates, key=lambda p: p.confidence)
        plate_box = best.box.padded(PLATE_CROP_PADDING_PX, vehicle_box.width, vehicle_box.height)
        if plate_box.area <= 0:
            return None, None

        plate_crop = crop[plate_box.y1 : plate_box.y2, plate_box.x1 : plate_box.x2]
        in_frame = plate_box.shifted(vehicle_box.x1, vehicle_box.y1).clamped(width, height)

        if not _ocr_slots.acquire(blocking=False):
            # Shed rather than queue — see OCR_CONCURRENCY. The plate box is
            # still returned, so the track keeps its geometry.
            self.metrics.count("ocr_shed")
            return None, in_frame

        try:
            with self.metrics.stage("ocr"):
                result = self.ocr.read(plate_crop)
        finally:
            _ocr_slots.release()

        if result is None or not result.text.strip():
            return None, in_frame

        # Burnt-in furniture check, on the frame-absolute position. The overlay
        # banner sits inside a large motion blob, so confining OCR to the
        # vehicle box does not exclude it — see services/anpr/overlay.py.
        text = result.text.strip()
        if self.overlay.is_static(in_frame.x1, in_frame.y1, text, now):
            self.overlay.note_suppressed(in_frame.x1, in_frame.y1, text, now)
            self.metrics.count("overlay_suppressed")
            return None, in_frame
        self.overlay.observe(in_frame.x1, in_frame.y1, text, track_id, now)

        # Confidence carries both stages: a confident read of something that
        # was probably not a plate is not a confident sighting.
        return (
            PlateRead(raw=result.text, confidence=result.confidence * best.confidence),
            in_frame,
        )

    # --- lifecycle ---

    def flush(self) -> list[CompletedTrack]:
        """End every open track. Called when the stream stops."""
        return self.registry.flush()

    @staticmethod
    def _frame_size(frame: Any) -> tuple[int, int]:
        shape = getattr(frame, "shape", None)
        if shape and len(shape) >= 2:
            return int(shape[0]), int(shape[1])
        return (0, 0)
