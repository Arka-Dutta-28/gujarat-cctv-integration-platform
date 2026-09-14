"""Keeping the picture that proves the read.

A plate string on its own is an assertion. The deliverables make the picture
mandatory rather than decorative: the M5 alert must arrive "with crop, camera,
time and map pin attached", and the M6 detection report is specified down to the
thumbnail. An operator deciding whether to stop a vehicle is deciding on the
image, not on the platform's confidence figure.

What is kept. One JPEG per sighting, cropped to the vehicle, taken from the
frame whose plate read best, rather than the last frame, which is usually the
vehicle half out of shot. The plate box is drawn on it, so the crop shows what
the pipeline read and not merely where the car was, and a reviewer can see
immediately when the box is around a bumper sticker.

What is not kept. No full frames and no video. A frame at 1080p is about 200 kB
against a crop's 8 kB, and at 104 sightings a minute the difference is 1.2 GB a
day against 48 MB. Video stays on the camera and is pulled on demand, which is
the platform's whole edge-first argument; storing frames here would quietly
contradict it.

Privacy. These are vehicle crops from public-space cameras, held under the same
retention as the sighting row that points at them. prune() is what makes the
retention claim real rather than a paragraph in a document.
"""

from __future__ import annotations

import logging
import os
import pathlib
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("anpr.crops")

__all__ = ["CROP_ROOT", "JPEG_QUALITY", "encode", "relative_path", "save", "prune"]

#: Shared volume, mounted read-write by the ANPR workers and read-only by the
#: API that serves the images. A path, not a URL: the API decides how it is
#: addressed, and nothing in the database holds a hostname.
CROP_ROOT = pathlib.Path(os.environ.get("ANPR_CROP_ROOT", "/data/crops"))

#: Enough to read a plate on screen, small enough that a day of the estate fits
#: in tens of megabytes. Measured: 8 kB at 75 for a typical vehicle crop.
JPEG_QUALITY = int(os.environ.get("ANPR_CROP_QUALITY", "75"))

#: Longest side of the stored crop. A vehicle box from a 1080p frame can be
#: 600 px across; the report and the alert card both render it far smaller.
MAX_EDGE_PX = int(os.environ.get("ANPR_CROP_MAX_PX", "480"))


def encode(image: Any, plate_box: Any = None) -> bytes | None:
    """JPEG bytes for one crop, with the plate box drawn if it is known.

    Returns None rather than raising: a sighting with no crop is a small loss,
    and an exception on this path would cost the read itself.
    """
    try:
        import cv2

        canvas = image
        if plate_box is not None:
            canvas = image.copy()
            cv2.rectangle(
                canvas,
                (plate_box.x1, plate_box.y1), (plate_box.x2, plate_box.y2),
                (0, 220, 255), 2,
            )

        height, width = canvas.shape[:2]
        longest = max(height, width)
        if longest > MAX_EDGE_PX:
            scale = MAX_EDGE_PX / longest
            canvas = cv2.resize(
                canvas, (max(1, int(width * scale)), max(1, int(height * scale))),
                interpolation=cv2.INTER_AREA,
            )

        ok, buffer = cv2.imencode(".jpg", canvas, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        return buffer.tobytes() if ok else None
    except Exception:  # noqa: BLE001 - never cost a read
        log.exception("crop encode failed")
        return None


def relative_path(camera_id: str, track_id: str, when: datetime | None = None) -> str:
    """`YYYY/MM/DD/<camera>/<track>.jpg`.

    Dated directories first so retention is a directory walk rather than a scan
    of every file's mtime, and so a day's crops can be dropped in one operation.
    Camera beneath the date, because pruning by age happens far more often than
    fetching everything one camera ever saw.
    """
    stamp = when or datetime.now(UTC)
    safe_track = "".join(c if c.isalnum() or c in "-_" else "-" for c in track_id)[:64]
    return f"{stamp:%Y/%m/%d}/{camera_id}/{safe_track or 'unknown'}.jpg"


def save(data: bytes, camera_id: str, track_id: str, when: datetime | None = None) -> str | None:
    """Write one crop and return the path to store on the sighting row."""
    rel = relative_path(camera_id, track_id, when)
    try:
        target = CROP_ROOT / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return rel
    except Exception:  # noqa: BLE001 - a missing crop must not lose a sighting
        log.exception("crop write failed for %s", rel)
        return None


def prune(days: int, root: pathlib.Path | None = None) -> int:
    """Delete crop directories older than `days`. Returns directories removed.

    Retention on the images is separate from retention on the rows: the sighting
    is a few hundred bytes and is what a trace needs, while the crop is the bulk.
    Keeping the row after its picture has aged out is the honest trade — the
    trace still works and the record says the crop is gone.
    """
    import shutil

    base = root or CROP_ROOT
    if not base.is_dir():
        return 0

    cutoff = datetime.now(UTC).date()
    removed = 0
    for year in sorted(base.iterdir()):
        for month in sorted(p for p in year.iterdir() if year.is_dir()):
            for day in sorted(p for p in month.iterdir() if month.is_dir()):
                try:
                    stamp = datetime.strptime(
                        f"{year.name}-{month.name}-{day.name}", "%Y-%m-%d"
                    ).date()
                except ValueError:
                    continue
                if (cutoff - stamp).days > days:
                    shutil.rmtree(day, ignore_errors=True)
                    removed += 1
    return removed
