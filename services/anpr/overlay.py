"""Reject burnt-in overlay text that the pipeline mistook for a plate.

Every government feed in this estate carries burnt-in furniture: a timestamp
band, a camera device label (CSITMS-32_PTZ2), a violation-detection tag (RLVD),
a site name. It is rendered crisply, at plate-like size and contrast, by a
machine, which makes it easier to read than a real plate seen at distance
through weather.

The pipeline was already designed against this: OCR is confined to a detected
vehicle box by construction rather than by a filter. On real footage that
protection failed, and the reason is worth recording. Motion proposals on a live
feed are not tidy car-shaped rectangles. Camera shake, a PTZ nudge, changing
light and crowds produce blobs spanning a third of the frame, and a blob whose
lower edge reaches the frame edge contains the overlay banner. Measured on the
31 government cameras: 422 sightings, zero structurally valid plates, and an
index filling with RLVD, PTZ1, CSI and 2026 at confidence 0.84, higher than any
genuine plate, so the overlay text also outranked real reads in the per-track
vote.

The discriminator here is deliberately not position alone. On a fixed camera
watching one lane, real plates also recur in roughly the same part of the frame,
so suppressing a hot region would throw away exactly the reads we want. What
burnt-in text does that a plate cannot is appear at the same place, with the
same characters, across many independent vehicle tracks. A real plate belongs to
one vehicle and therefore to one track; overlay text gets attributed to whichever
motion blob happens to overlap it that second, so it accumulates across
unrelated tracks.

Persistence means continuously present, and the filter forgets text it has
stopped seeing. Burnt-in text is re-read every few seconds for as long as
anything moves over it; a plate is read for the few seconds its vehicle is in
view and then not at all. Without that forgetting, the rule "three tracks, sixty
seconds apart" is satisfied by any vehicle that comes back: the same bus on its
timetable, a commuter every morning, and, measured on 6-7 Sep 2026, every
vehicle in a looping clip. The simulated streams loop in under 75 s, so each
plate returned under a new track id about once a minute and was declared
furniture after its third pass. Plate reads across the estate went from 168 in
the first quarter hour after a restart to zero within 3.5 hours, and stayed at
zero for seven days while the filter suppressed 98.5% of everything OCR read. No
alert fired and the demo plate's trace stopped growing, with every health check
green.

That property is self-calibrating: it needs no per-camera configuration, no
hand-drawn masks, and it works wherever the overlay sits. It also degrades
safely, because the first few reads of a new overlay are admitted, flagged
format-invalid, and kept, which is what the keep-and-flag convention requires.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

__all__ = ["StaticTextFilter", "CELL_PX", "MIN_TRACKS", "MIN_SPAN_S", "MAX_GAP_S"]

#: Grid resolution for "the same place". Coarse enough that an overlay jitters
#: within one cell, fine enough that two lanes are distinguishable.
CELL_PX = int(os.environ.get("ANPR_OVERLAY_CELL_PX", "48"))

#: How many *distinct tracks* must produce identical text in one cell before it
#: is called furniture. Three, because two can legitimately collide: a plate
#: mis-read the same way on two vehicles in one lane is unlikely but possible,
#: and the cost of a false suppression is a vehicle we never report.
MIN_TRACKS = int(os.environ.get("ANPR_OVERLAY_MIN_TRACKS", "3"))

#: And it must persist. A convoy of identical fleet plates passing in ten
#: seconds is traffic; the same characters still there two minutes later is
#: painted on.
MIN_SPAN_S = float(os.environ.get("ANPR_OVERLAY_MIN_SPAN_S", "60"))

#: The longest silence a region may keep and still be the same furniture. Text
#: unseen for longer starts again from nothing.
#:
#: 30 s, from measurement on 14 Sep 2026. The live simulated streams repeat each
#: plate every 54-74 s (cam-46 54.2, cam-10 55.8, cam-34 74.0), so at 60 s cam-10
#: still suppressed 94 real plate reads in five minutes; at 30 s it suppressed
#: none and voted all 8 of its plates 5-6 times.
#:
#: Limit: one global gap. A burnt-in label on a quiet camera that is read less
#: often than every 30 s will never be learned and will leak, kept and flagged
#: format-invalid. Unmeasurable while the government grid is locked (the estate
#: average was one overlay read per ~22 s on 18 Aug). If it leaks, raise this per
#: deployment, or learn the gap per camera from how often its text is read.
MAX_GAP_S = float(os.environ.get("ANPR_OVERLAY_MAX_GAP_S", "30"))

#: Bound on distinct (cell, text) keys held per camera, so a noisy feed cannot
#: grow this without limit. Evicted oldest-first.
MAX_KEYS = 4096


@dataclass
class _Occurrence:
    tracks: set[str] = field(default_factory=set)
    first_seen: float = 0.0
    last_seen: float = 0.0
    suppressed: int = 0


class StaticTextFilter:
    """Per-camera memory of text that stays put while vehicles do not.

    Not thread-safe by design: one instance belongs to one camera's pipeline,
    which is one decode thread.
    """

    def __init__(
        self,
        cell_px: int = CELL_PX,
        min_tracks: int = MIN_TRACKS,
        min_span_s: float = MIN_SPAN_S,
        max_gap_s: float = MAX_GAP_S,
    ) -> None:
        self.cell_px = max(1, cell_px)
        self.min_tracks = max(2, min_tracks)
        self.min_span_s = min_span_s
        self.max_gap_s = max_gap_s
        self._seen: dict[tuple[int, int, str], _Occurrence] = {}
        #: Latest time seen by any call, so reporting can tell stale from live.
        self._now = 0.0

    def _key(self, x: int, y: int, text: str) -> tuple[int, int, str]:
        return (x // self.cell_px, y // self.cell_px, text.strip().upper())

    def observe(self, x: int, y: int, text: str, track_id: str, now: float) -> None:
        """Record that `text` was read at frame position (x, y) by `track_id`."""
        if not text.strip():
            return
        self._now = max(self._now, now)
        key = self._key(x, y, text)
        entry = self._seen.get(key)
        if entry is not None and now - entry.last_seen > self.max_gap_s:
            entry = None  # gone quiet: whatever comes back is new evidence
        if entry is None:
            if len(self._seen) >= MAX_KEYS:
                # Oldest by last activity. Cheap because this is rare.
                oldest = min(self._seen, key=lambda k: self._seen[k].last_seen)
                del self._seen[oldest]
            entry = _Occurrence(first_seen=now)
            self._seen[key] = entry
        entry.tracks.add(track_id)
        entry.last_seen = now

    def _proven(self, entry: _Occurrence, now: float) -> bool:
        return (
            len(entry.tracks) >= self.min_tracks
            and (entry.last_seen - entry.first_seen) >= self.min_span_s
            and now - entry.last_seen <= self.max_gap_s
        )

    def is_static(self, x: int, y: int, text: str, now: float) -> bool:
        """Whether this text at this place has proved to be painted on."""
        self._now = max(self._now, now)
        entry = self._seen.get(self._key(x, y, text))
        return entry is not None and self._proven(entry, now)

    def note_suppressed(self, x: int, y: int, text: str, now: float) -> None:
        """Count a suppression, and keep the region live while it is read.

        Suppressed text is never passed to `observe`, so without this refresh
        a real overlay would fall silent after `max_gap_s` and be re-learned.
        """
        entry = self._seen.get(self._key(x, y, text))
        if entry is not None:
            entry.suppressed += 1
            entry.last_seen = max(entry.last_seen, now)

    @property
    def known_overlays(self) -> list[tuple[int, int, str, int]]:
        """Every region judged to be furniture, for reporting.

        A suppression nobody can inspect is indistinguishable from a bug, and
        this list is what lets an operator confirm the platform is ignoring the
        camera's timestamp rather than a lane of traffic.
        """
        return [
            (cell_x * self.cell_px, cell_y * self.cell_px, text, entry.suppressed)
            for (cell_x, cell_y, text), entry in self._seen.items()
            if self._proven(entry, self._now)
        ]
