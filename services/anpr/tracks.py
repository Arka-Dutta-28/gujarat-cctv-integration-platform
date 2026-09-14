"""Track lifecycle: turning frames into one sighting per vehicle.

A tracker gives each vehicle an id that persists across frames. This module
holds what has been seen of each track and decides when the track is over and
its accumulated reads should be collapsed into a single sighting.

Three ways a track ends, and all three matter.

It leaves the frame. The normal case. Nothing has referenced the track for a
couple of seconds, so it is finished and voted on.

It has been there too long. A vehicle parked in view would otherwise hold its
sighting open indefinitely, and a watchlist hit written only when the car
finally drives away is a watchlist hit that arrived too late to be worth having.
Long tracks are cut and emitted, and carry on as a new segment.

The stream stops. Everything open is flushed rather than lost.

Kept free of any dependency on the detector or the tracker library, so the
policy that decides what becomes a sighting can be tested on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.anpr.vote import PlateRead, TrackVote, vote
from services.common.plates import normalise_plate

__all__ = [
    "Track", "TrackRegistry", "IDLE_TIMEOUT_S", "MAX_TRACK_DURATION_S",
    "MIN_READS", "MAX_READS",
]

#: How long a track can go unreferenced before it is considered gone. Long
#: enough to survive a vehicle passing behind a pole, short enough that the
#: sighting is written while the vehicle is still nearby.
IDLE_TIMEOUT_S = 2.0

#: A track is cut and emitted at this age even if the vehicle is still there.
#: Alerting is only useful if it is timely.
MAX_TRACK_DURATION_S = 30.0

#: Reads to gather before the vote is allowed to settle early. Below this a
#: single confident misread could carry the whole track.
MIN_READS = 3

#: And the point past which more reads buy nothing. The repetition bonus in the
#: vote saturates at 8 by construction, so a ninth read of an agreeing plate
#: cannot change either the answer or the confidence — it is pure cost. OCR is
#: by far the most expensive stage (p50 86 ms uncontended, and it degrades
#: badly under load because each call is a subprocess), so this cap is the
#: difference between an estate that keeps up and one that falls behind.
MAX_READS = 8

#: Agreeing reads at this confidence are enough to stop early.
SETTLED_CONFIDENCE = 0.75


@dataclass
class Track:
    track_id: str
    camera_id: str
    first_seen: float
    last_seen: float
    frames: int = 0
    reads: list[PlateRead] = field(default_factory=list)
    vehicle_class: str | None = None
    #: Box from the frame where the plate read best — the crop worth keeping
    #: for the report, rather than whichever frame happened to be last.
    bbox: tuple[int, int, int, int] | None = None
    plate_bbox: tuple[int, int, int, int] | None = None
    condition: str | None = None
    slot_offset: float | None = None
    best_read_confidence: float = 0.0
    crop_path: str | None = None
    #: JPEG of the vehicle from the frame that read best, with the plate box
    #: drawn on it. Held encoded rather than as an array: a track can live 30
    #: seconds across dozens of frames, and one worker carries hundreds of
    #: tracks — 8 kB of JPEG against ~90 kB of pixels, per track.
    crop_jpeg: bytes | None = None
    #: Appearance descriptor from the same frame, for re-identification across
    #: cameras when the plate could not be read (services/anpr/reid.py).
    embedding: list[float] | None = None
    #: What a person would call this vehicle (services/anpr/attributes.py).
    #: Kept from the *largest* view of the vehicle rather than the best-reading
    #: one, because those are different frames and they are chosen for
    #: different reasons: a plate reads best when it is square-on, while colour
    #: is most reliable when the most bodywork is visible. On a camera that
    #: never reads a plate at all — 30 of the 30 government feeds — there is no
    #: best-reading frame to fall back on, so this has to be tracked
    #: independently or those vehicles would carry no description either.
    vehicle_colour: str | None = None
    colour_confidence: float = 0.0
    #: Area of the largest box seen, so a later frame only replaces the
    #: description when it is a genuinely better view.
    best_box_area: int = 0
    #: Pixels of the largest view, embedded by `reid.embed_many` when the track
    #: finishes and then dropped. `appearance` is that vector.
    best_crop: Any = None
    appearance: list[float] | None = None

    @property
    def duration_s(self) -> float:
        return self.last_seen - self.first_seen

    def needs_more_reads(self) -> bool:
        """Whether another OCR pass on this vehicle is worth its cost.

        The vote is a diminishing return by construction: its repetition bonus
        saturates, and reads that agree stop moving the answer. Once a track has
        settled there is nothing left to buy, and OCR is the most expensive
        stage in the pipeline — so this is where the analytics budget is spent
        or wasted.
        """
        if len(self.reads) < MIN_READS:
            return True
        if len(self.reads) >= MAX_READS:
            return False
        # Settled: the recent reads agree and they were confident.
        recent = self.reads[-MIN_READS:]
        agreed = len({normalise_plate(r.raw) for r in recent}) == 1
        return not (agreed and min(r.confidence for r in recent) >= SETTLED_CONFIDENCE)

    def observe(
        self,
        now: float,
        *,
        read: PlateRead | None = None,
        vehicle_class: str | None = None,
        bbox: tuple[int, int, int, int] | None = None,
        plate_bbox: tuple[int, int, int, int] | None = None,
        condition: str | None = None,
        slot_offset: float | None = None,
        crop_jpeg: bytes | None = None,
        embedding: list[float] | None = None,
        attributes: Any = None,
    ) -> None:
        self.last_seen = now
        self.frames += 1
        if vehicle_class:
            self.vehicle_class = vehicle_class
        if condition:
            self.condition = condition
        if slot_offset is not None:
            self.slot_offset = slot_offset
        if attributes is not None and bbox:
            self._describe(attributes, bbox)
        if read is not None and read.raw:
            self.reads.append(read)
            # Keep the geometry from the frame that read best, not the latest.
            if read.confidence >= self.best_read_confidence:
                self.best_read_confidence = read.confidence
                if bbox:
                    self.bbox = bbox
                if plate_bbox:
                    self.plate_bbox = plate_bbox
                # The evidence image follows the same rule as the geometry: keep
                # the frame that read best, not the one that happened to be last
                # — which is usually the vehicle half out of shot.
                if crop_jpeg:
                    self.crop_jpeg = crop_jpeg
                if embedding:
                    self.embedding = embedding
        elif self.bbox is None and bbox:
            self.bbox = bbox

    def _describe(self, attributes: Any, bbox: tuple[int, int, int, int]) -> None:
        """Keep the description from the largest view of this vehicle.

        Largest, not most recent and not best-reading. A track's last frame is
        usually the vehicle half out of shot, and its best-reading frame is
        chosen for plate geometry; neither is the frame where the most bodywork
        is visible. Colour is a proportion of visible bodywork, so the biggest
        box is the one whose answer should survive.
        """
        x1, y1, x2, y2 = bbox
        area = max(0, x2 - x1) * max(0, y2 - y1)
        if area < self.best_box_area:
            return
        self.best_box_area = area
        colour = getattr(attributes, "colour", None)
        if colour:
            self.vehicle_colour = colour
            self.colour_confidence = float(getattr(attributes, "colour_confidence", 0.0))
        embedding = getattr(attributes, "embedding", None)
        if embedding:
            # From the same frame as the colour, by construction. Before 6 Sep
            # 2026 this was kept from whichever frame read the plate best,
            # which meant a vehicle whose plate was never read carried no
            # descriptor at all — switching re-ID off on exactly the cameras
            # that need it, since they are the ones that cannot read plates.
            self.embedding = embedding
        crop = getattr(attributes, "crop", None)
        if crop is not None:
            self.best_crop = crop
        # `vehicle_class` is deliberately *not* taken from the attributes here.
        # The column has stored the detector's own label since M3 and the
        # reports, the journey view and the capability grading all read it that
        # way; rewriting it to the friendlier name would silently change what
        # every existing row means. The mapping to what a person would say
        # (`motorcycle` -> `two-wheeler`) is applied where it is needed — at
        # match time and at render time — so there is one stored truth.


@dataclass
class CompletedTrack:
    """A finished track and the single sighting it voted for.

    `result` is None when the vehicle was tracked but never produced a readable
    plate — common, and not a fault. It is still counted, because "how many
    vehicles did we see versus how many plates did we read" is the honest
    denominator for any accuracy claim.
    """

    track: Track
    result: TrackVote | None
    reason: str


class TrackRegistry:
    """Open tracks for one camera."""

    def __init__(
        self,
        camera_id: str,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
        max_duration_s: float = MAX_TRACK_DURATION_S,
    ) -> None:
        self.camera_id = camera_id
        self.idle_timeout_s = idle_timeout_s
        self.max_duration_s = max_duration_s
        self.tracks: dict[str, Track] = {}
        self.completed = 0

    def observe(self, track_id: str, now: float, **kwargs: object) -> Track:
        track = self.tracks.get(track_id)
        if track is None:
            track = Track(
                track_id=track_id, camera_id=self.camera_id,
                first_seen=now, last_seen=now,
            )
            self.tracks[track_id] = track
        track.observe(now, **kwargs)  # type: ignore[arg-type]
        return track

    @property
    def active(self) -> int:
        return len(self.tracks)

    def harvest(self, now: float) -> list[CompletedTrack]:
        """Return every track that has finished, and forget it.

        A track cut for length is emitted and immediately restarted, so a
        vehicle sitting in view produces a sighting every `max_duration_s`
        rather than one enormous one at the end.
        """
        done: list[CompletedTrack] = []
        for track_id, track in list(self.tracks.items()):
            if now - track.last_seen >= self.idle_timeout_s:
                reason = "left frame"
            elif track.duration_s >= self.max_duration_s:
                reason = "max duration"
            else:
                continue

            done.append(CompletedTrack(track=track, result=vote(track.reads), reason=reason))
            self.completed += 1
            del self.tracks[track_id]

            if reason == "max duration":
                # Same vehicle, new segment: keep tracking, start the vote over.
                self.tracks[track_id] = Track(
                    track_id=track_id, camera_id=self.camera_id,
                    first_seen=now, last_seen=track.last_seen,
                    vehicle_class=track.vehicle_class, condition=track.condition,
                )
        return done

    def flush(self) -> list[CompletedTrack]:
        """End every open track. Called when a stream stops."""
        done = [
            CompletedTrack(track=t, result=vote(t.reads), reason="stream ended")
            for t in self.tracks.values()
        ]
        self.completed += len(done)
        self.tracks.clear()
        return done
