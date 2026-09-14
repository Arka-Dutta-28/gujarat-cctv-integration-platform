"""What the pipeline needs from a detector, a plate locator and an OCR engine.

Narrow protocols rather than concrete classes, for the same reason the camera
adapters are: the models are the part most likely to be swapped — for a better
one, for a GPU build, for whatever runs on the edge box — and none of that
should reach the pipeline. It also means the pipeline's logic can be tested
without loading a single weight file.

Boxes are integer pixel coordinates in frame space throughout. Converting
between conventions in three places is how bounding-box bugs happen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "Box", "VehicleDetection", "PlateDetection", "OcrResult",
    "VehicleTracker", "PlateLocator", "PlateOcr",
]

#: COCO classes that are vehicles. Bicycles are deliberately excluded: they
#: carry no plate, and tracking them only costs OCR attempts that cannot succeed.
VEHICLE_CLASSES = frozenset({"car", "motorcycle", "bus", "truck"})


@dataclass(frozen=True)
class Box:
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def width(self) -> int:
        return max(0, self.x2 - self.x1)

    @property
    def height(self) -> int:
        return max(0, self.y2 - self.y1)

    @property
    def area(self) -> int:
        return self.width * self.height

    def clamped(self, width: int, height: int) -> Box:
        """Keep the box inside the frame. A crop that runs off the edge is empty."""
        return Box(
            max(0, min(self.x1, width)), max(0, min(self.y1, height)),
            max(0, min(self.x2, width)), max(0, min(self.y2, height)),
        )

    def padded(self, pixels: int, width: int, height: int) -> Box:
        """Grow the box, then clamp. Plate detectors cut fine; OCR wants margin."""
        return Box(
            self.x1 - pixels, self.y1 - pixels, self.x2 + pixels, self.y2 + pixels
        ).clamped(width, height)

    def shifted(self, dx: int, dy: int) -> Box:
        """Move from crop space back into frame space."""
        return Box(self.x1 + dx, self.y1 + dy, self.x2 + dx, self.y2 + dy)

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x1, self.y1, self.x2, self.y2)


@dataclass(frozen=True)
class VehicleDetection:
    box: Box
    #: Assigned by the tracker and stable across frames. None means the tracker
    #: detected a vehicle but could not associate it — no track, so no vote.
    track_id: str | None
    label: str
    confidence: float


@dataclass(frozen=True)
class PlateDetection:
    #: In the coordinate space of whatever image was passed in.
    box: Box
    confidence: float


@dataclass(frozen=True)
class OcrResult:
    text: str
    confidence: float


class VehicleTracker(Protocol):
    """Detects vehicles and keeps an id on each across frames."""

    def track(self, frame: Any) -> list[VehicleDetection]: ...

    def reset(self) -> None:
        """Forget all track state — called when a stream reconnects."""


class PlateLocator(Protocol):
    """Finds plate regions inside an image, usually a vehicle crop."""

    def locate(self, image: Any) -> list[PlateDetection]: ...


class PlateOcr(Protocol):
    """Reads the characters off a plate crop."""

    def read(self, image: Any) -> OcrResult | None: ...
