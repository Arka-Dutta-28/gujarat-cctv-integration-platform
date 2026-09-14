"""ANPR pipeline ordering and the invariants it must not break.

Run entirely on stubs. The point is not to test a detector — it is to test the
things that would silently ruin the results if they were wrong: that OCR never
sees anything outside a vehicle box, that every readable track becomes a
sighting, and that a vehicle we could not read is still counted.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import pytest

from services.anpr.models import Box, OcrResult, PlateDetection, VehicleDetection
from services.anpr.pipeline import AnprPipeline, FrameContext
from services.anpr.sampling import AdaptiveSampler, SamplerConfig


class FakeFrame:
    """A frame that records which regions were cropped out of it."""

    def __init__(self, width: int = 1920, height: int = 1080, origin: tuple[int, int] = (0, 0)):
        self.shape = (height, width, 3)
        self.origin = origin
        self.crops: list[tuple[int, int, int, int]] = []

    def __getitem__(self, key: tuple[slice, slice]) -> FakeFrame:
        ys, xs = key
        x0, y0 = self.origin
        self.crops.append((xs.start, ys.start, xs.stop, ys.stop))
        child = FakeFrame(
            width=max(0, xs.stop - xs.start),
            height=max(0, ys.stop - ys.start),
            origin=(x0 + xs.start, y0 + ys.start),
        )
        child.crops = self.crops
        return child


@dataclass
class FakeTracker:
    detections: list[VehicleDetection] = field(default_factory=list)
    reset_calls: int = 0

    def track(self, frame: object) -> list[VehicleDetection]:  # noqa: ARG002
        return self.detections

    def reset(self) -> None:
        self.reset_calls += 1


@dataclass
class FakeLocator:
    plates: list[PlateDetection] = field(default_factory=list)
    seen: list[FakeFrame] = field(default_factory=list)

    def locate(self, image: FakeFrame) -> list[PlateDetection]:
        self.seen.append(image)
        return self.plates


@dataclass
class FakeOcr:
    result: OcrResult | None = None
    seen: list[FakeFrame] = field(default_factory=list)

    def read(self, image: FakeFrame) -> OcrResult | None:
        self.seen.append(image)
        return self.result


def build(
    *,
    vehicles: list[VehicleDetection] | None = None,
    plates: list[PlateDetection] | None = None,
    ocr: OcrResult | None = None,
) -> tuple[AnprPipeline, FakeLocator, FakeOcr]:
    locator = FakeLocator(plates=plates if plates is not None else [
        PlateDetection(box=Box(20, 60, 90, 80), confidence=0.9)
    ])
    reader = FakeOcr(result=ocr if ocr is not None else OcrResult("GJ01AB1234", 0.9))
    pipeline = AnprPipeline(
        camera_id="cam",
        tracker=FakeTracker(detections=vehicles if vehicles is not None else [
            VehicleDetection(box=Box(400, 300, 700, 560), track_id="t1",
                             label="car", confidence=0.9)
        ]),
        locator=locator,
        ocr=reader,
        # Analyse every frame, so these tests are about ordering not sampling.
        sampler=AdaptiveSampler(config=SamplerConfig(max_hz=1000, min_hz=1000)),
    )
    return pipeline, locator, reader


class TestOcrStaysInsideVehicleBoxes:
    """Burnt-in overlays are read as plates unless the search is confined."""

    def test_the_plate_search_only_ever_sees_a_vehicle_crop(self) -> None:
        pipeline, locator, _ = build()
        frame = FakeFrame()
        pipeline.process(frame, FrameContext(now=0.0))
        assert locator.seen
        # The locator was handed the vehicle box, not the frame.
        assert locator.seen[0].origin == (400, 300)
        assert locator.seen[0].shape[:2] == (260, 300)

    def test_ocr_only_ever_sees_a_region_inside_that_crop(self) -> None:
        pipeline, _, reader = build()
        pipeline.process(FakeFrame(), FrameContext(now=0.0))
        assert reader.seen
        ox, oy = reader.seen[0].origin
        assert 400 <= ox <= 700
        assert 300 <= oy <= 560

    def test_nothing_is_read_when_no_vehicle_is_detected(self) -> None:
        """The whole-frame overlay must never be reached."""
        pipeline, locator, reader = build(vehicles=[])
        pipeline.process(FakeFrame(), FrameContext(now=0.0))
        assert locator.seen == []
        assert reader.seen == []

    def test_a_detection_the_tracker_could_not_associate_is_not_read(self) -> None:
        pipeline, locator, _ = build(vehicles=[
            VehicleDetection(box=Box(400, 300, 700, 560), track_id=None,
                             label="car", confidence=0.9)
        ])
        pipeline.process(FakeFrame(), FrameContext(now=0.0))
        assert locator.seen == []

    def test_a_bicycle_is_not_searched_for_a_plate(self) -> None:
        pipeline, locator, _ = build(vehicles=[
            VehicleDetection(box=Box(400, 300, 700, 560), track_id="t1",
                             label="bicycle", confidence=0.9)
        ])
        pipeline.process(FakeFrame(), FrameContext(now=0.0))
        assert locator.seen == []


class TestEveryReadIsPersisted:
    """The project's most important invariant, checked at the pipeline seam."""

    def test_a_readable_track_becomes_a_sighting(self) -> None:
        pipeline, _, _ = build()
        frame = FakeFrame()
        for i in range(10):
            pipeline.process(frame, FrameContext(now=i * 0.1))
        done = pipeline.flush()
        assert len(done) == 1
        assert done[0].result is not None
        assert done[0].result.plate_normalised == "GJ01AB1234"

    def test_a_plate_that_fails_the_format_check_is_still_a_sighting(self) -> None:
        pipeline, _, _ = build(ocr=OcrResult("NOTAPLATE!!", 0.8))
        frame = FakeFrame()
        for i in range(6):
            pipeline.process(frame, FrameContext(now=i * 0.1))
        done = pipeline.flush()
        assert done[0].result is not None
        assert done[0].result.format_valid is False

    def test_a_vehicle_with_no_plate_found_is_still_counted(self) -> None:
        pipeline, _, _ = build(plates=[])
        pipeline.process(FakeFrame(), FrameContext(now=0.0))
        done = pipeline.flush()
        assert len(done) == 1
        assert done[0].result is None
        assert done[0].track.vehicle_class == "car"

    def test_a_vehicle_too_small_to_read_is_tracked_not_dropped(self) -> None:
        """Otherwise the read rate is quietly measured against a smaller estate."""
        pipeline, locator, _ = build(vehicles=[
            VehicleDetection(box=Box(100, 100, 120, 120), track_id="t1",
                             label="car", confidence=0.9)
        ])
        pipeline.process(FakeFrame(), FrameContext(now=0.0))
        assert locator.seen == []
        assert pipeline.flush()[0].track.vehicle_class == "car"


class TestConfidenceAndContext:
    def test_plate_detection_confidence_discounts_the_ocr_confidence(self) -> None:
        """A confident read of something that was probably not a plate is not confident."""
        pipeline, _, _ = build(
            plates=[PlateDetection(box=Box(20, 60, 90, 80), confidence=0.5)],
            ocr=OcrResult("GJ01AB1234", 0.8),
        )
        pipeline.process(FakeFrame(), FrameContext(now=0.0))
        track = pipeline.registry.tracks["t1"]
        assert track.reads[0].confidence == 0.4

    def test_the_slot_offset_and_condition_reach_the_sighting(self) -> None:
        pipeline, _, _ = build()
        pipeline.process(FakeFrame(), FrameContext(now=0.0, condition="glare", slot_offset=4978.5))
        track = pipeline.flush()[0].track
        assert track.condition == "glare"
        assert track.slot_offset == 4978.5

    def test_the_plate_box_is_reported_in_frame_coordinates(self) -> None:
        """A box in crop space would draw the evidence marker in the wrong place."""
        pipeline, _, _ = build()
        pipeline.process(FakeFrame(), FrameContext(now=0.0))
        plate_box = pipeline.flush()[0].track.plate_bbox
        assert plate_box is not None
        assert plate_box[0] >= 400
        assert plate_box[1] >= 300


class TestSampling:
    def test_frames_the_sampler_skips_do_not_reach_the_detector(self) -> None:
        pipeline, locator, _ = build()
        pipeline.sampler = AdaptiveSampler(config=SamplerConfig(max_hz=2, min_hz=0.5))
        frame = FakeFrame()
        for i in range(150):  # ten seconds at 15 fps
            pipeline.process(frame, FrameContext(now=i / 15))
        assert len(locator.seen) < 40
        assert pipeline.metrics.counters["frames_decoded"] == 150

    def test_a_finished_track_is_harvested_even_on_a_skipped_frame(self) -> None:
        """Otherwise a sighting waits for the next sampled frame to be written."""
        pipeline, _, _ = build()
        pipeline.sampler = AdaptiveSampler(config=SamplerConfig(max_hz=1000, min_hz=1000))
        frame = FakeFrame()
        pipeline.process(frame, FrameContext(now=0.0))
        pipeline.sampler = AdaptiveSampler(
            config=SamplerConfig(max_hz=0.001, min_hz=0.001, max_gap_s=1e9)
        )
        pipeline.sampler.last_analysed_at = 0.0
        done = pipeline.process(frame, FrameContext(now=10.0))
        assert len(done) == 1


class TestWarmupIsNotSteadyState:
    def test_the_first_inference_is_timed_separately(self) -> None:
        """A 252-second CUDA cold start averaged into p95 makes it meaningless."""
        pipeline, _, _ = build()
        frame = FakeFrame()
        pipeline.process(frame, FrameContext(now=0.0))
        pipeline.process(frame, FrameContext(now=0.1))
        pipeline.process(frame, FrameContext(now=0.2))
        stages = {s.stage: s for s in pipeline.metrics.snapshot()}
        assert stages["model_warmup"].samples == 1
        assert stages["vehicle_detect"].samples == 2


class TestOcrBudget:
    def test_ocr_stops_once_the_vote_has_settled(self) -> None:
        """OCR degrades badly under load; a settled vote buys nothing more."""
        pipeline, _, reader = build()
        frame = FakeFrame()
        for i in range(40):
            pipeline.process(frame, FrameContext(now=i * 0.01))
        assert len(reader.seen) <= 8
        assert pipeline.metrics.counters.get("ocr_skipped_settled", 0) > 0

    def test_the_vehicle_is_still_tracked_while_ocr_is_skipped(self) -> None:
        pipeline, _, _ = build()
        frame = FakeFrame()
        for i in range(40):
            pipeline.process(frame, FrameContext(now=i * 0.01))
        done = pipeline.flush()
        assert len(done) == 1
        assert done[0].track.frames == 40
        assert done[0].result is not None


class TestOcrLoadShedding:
    def test_a_frame_that_cannot_get_an_ocr_slot_is_shed_not_queued(self) -> None:
        """A blocked decoder loses whole vehicles; a shed read loses one vote."""
        import services.anpr.pipeline as mod

        pipeline, _, reader = build()
        exhausted = threading.BoundedSemaphore(1)
        exhausted.acquire()
        original, mod._ocr_slots = mod._ocr_slots, exhausted
        try:
            pipeline.process(FakeFrame(), FrameContext(now=0.0))
        finally:
            mod._ocr_slots = original

        assert reader.seen == []
        assert pipeline.metrics.counters.get("ocr_shed") == 1

    def test_a_shed_frame_still_tracks_the_vehicle(self) -> None:
        import services.anpr.pipeline as mod

        pipeline, _, _ = build()
        exhausted = threading.BoundedSemaphore(1)
        exhausted.acquire()
        original, mod._ocr_slots = mod._ocr_slots, exhausted
        try:
            pipeline.process(FakeFrame(), FrameContext(now=0.0))
        finally:
            mod._ocr_slots = original

        done = pipeline.flush()
        assert len(done) == 1
        assert done[0].track.vehicle_class == "car"

    def test_the_slot_is_released_even_when_ocr_raises(self) -> None:
        """A leaked slot would silently halve throughput for the process life."""
        import services.anpr.pipeline as mod

        pipeline, _, reader = build()

        def boom(image: object) -> None:
            raise RuntimeError("model blew up")

        reader.read = boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            pipeline.process(FakeFrame(), FrameContext(now=0.0))
        assert mod._ocr_slots.acquire(blocking=False)
        mod._ocr_slots.release()
