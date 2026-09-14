"""A small IoU tracker, for when a CNN detector is not the right tool.

Fixed cameras are the majority of a CCTV estate, and on a fixed camera the
scene is nearly static. Background subtraction finds the moving regions for a
few hundred microseconds of arithmetic — no model, no GPU, no download — and
this associates those regions across frames into tracks.

That matters for two reasons beyond convenience. It is what lets the pipeline
keep working on a camera where the learned detector sees nothing (an unusual
mounting, a scene it was never trained on, a synthetic test feed), and at
80,000 cameras the difference between "a CNN on every analysed frame" and "a
CNN only where motion says it is worth one" is the difference between a
plausible deployment and an implausible one.

Association is IoU-first, centroid-second: overlap is the stronger signal when
a vehicle is large and slow, and distance rescues the case where it moved far
enough between analysed frames that the boxes no longer touch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.anpr.models import Box

__all__ = ["CentroidTracker", "iou", "centre_distance"]

#: Below this overlap, two boxes are not the same vehicle by IoU alone.
MIN_IOU = 0.2

#: How far a centre may move between analysed frames and still be the same
#: vehicle, as a fraction of the frame diagonal. At 6 Hz analysis a vehicle at
#: 60 km/h crosses ~3 m per step, which is well inside this for any usable view.
MAX_CENTRE_TRAVEL = 0.18

#: Frames a track survives without a matching detection before it is dropped.
MAX_MISSES = 8


def iou(a: Box, b: Box) -> float:
    """Intersection over union of two boxes."""
    x1, y1 = max(a.x1, b.x1), max(a.y1, b.y1)
    x2, y2 = min(a.x2, b.x2), min(a.y2, b.y2)
    overlap = max(0, x2 - x1) * max(0, y2 - y1)
    if overlap == 0:
        return 0.0
    union = a.area + b.area - overlap
    return overlap / union if union else 0.0


def centre_distance(a: Box, b: Box) -> float:
    ax, ay = (a.x1 + a.x2) / 2, (a.y1 + a.y2) / 2
    bx, by = (b.x1 + b.x2) / 2, (b.y1 + b.y2) / 2
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5


@dataclass
class _Tracked:
    track_id: str
    box: Box
    misses: int = 0
    hits: int = 1


@dataclass
class CentroidTracker:
    """Associates boxes across frames into stable track ids."""

    min_iou: float = MIN_IOU
    max_centre_travel: float = MAX_CENTRE_TRAVEL
    max_misses: int = MAX_MISSES
    next_id: int = 1
    tracks: dict[str, _Tracked] = field(default_factory=dict)

    def update(self, boxes: list[Box], frame_width: int, frame_height: int) -> list[str]:
        """Assign a track id to each box, in the order the boxes were given."""
        diagonal = (frame_width**2 + frame_height**2) ** 0.5 or 1.0
        max_travel = self.max_centre_travel * diagonal

        assigned: dict[int, str] = {}
        taken: set[str] = set()

        # Greedy over the strongest pairings first. A vehicle that overlaps its
        # own previous box is a surer match than one that merely moved a
        # plausible distance, so all the overlaps are settled before any of the
        # distance matches are considered.
        candidates = [
            (iou(box, t.box), -centre_distance(box, t.box), i, tid)
            for i, box in enumerate(boxes)
            for tid, t in self.tracks.items()
        ]
        candidates.sort(reverse=True)

        for overlap, neg_distance, index, track_id in candidates:
            if index in assigned or track_id in taken:
                continue
            if overlap < self.min_iou and -neg_distance > max_travel:
                continue
            assigned[index] = track_id
            taken.add(track_id)

        out: list[str] = []
        for i, box in enumerate(boxes):
            track_id = assigned.get(i)
            if track_id is None:
                track_id = str(self.next_id)
                self.next_id += 1
                self.tracks[track_id] = _Tracked(track_id=track_id, box=box)
            else:
                tracked = self.tracks[track_id]
                tracked.box = box
                tracked.misses = 0
                tracked.hits += 1
            out.append(track_id)

        # Age out anything that was not matched this frame. Kept for a few
        # frames rather than dropped at once, so a vehicle briefly lost behind a
        # pole keeps its identity instead of becoming a second sighting.
        for track_id, tracked in list(self.tracks.items()):
            if track_id in taken:
                continue
            tracked.misses += 1
            if tracked.misses > self.max_misses:
                del self.tracks[track_id]

        return out

    def reset(self) -> None:
        self.tracks.clear()
