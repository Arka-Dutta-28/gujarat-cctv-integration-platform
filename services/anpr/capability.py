"""Which cameras in the estate can actually read a plate.

This module exists because of a measurement, not a design idea. The 31
government feeds onboarded from the state's own middleware produced 422
sightings and zero structurally valid plates. The cause was not the pipeline:
their plate crops average 66 px across, against the simulated farm's 276 px,
which yields about 1.7 characters per read against 9.4. At 66 px for a whole
ten-character Indian plate, each character is roughly 7 px of sensor, below what
any recogniser can resolve, ours or anyone's.

They are not broken cameras. They are wide-area situational-awareness views,
correctly deployed for watching a junction and useless for reading a
registration number. A platform reporting one estate-wide accuracy figure
silently averages the two together and claims an ANPR capability on cameras that
physically do not have one.

So capability is derived from evidence the pipeline already recorded, per
camera, and reported. Two consequences follow, both deliberate:

  - An accuracy figure can be scoped to the cameras it is true of.
  - An operator planning coverage learns where a plate can be read, which is a
    different map from where there is a camera, and it is the map that decides
    whether a vehicle can be traced through a district at all.

Nothing here filters, hides or discards a read. Invariant 1 is untouched: every
read is still persisted and still findable. This module only labels the source.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = [
    "CameraEvidence",
    "Assessment",
    "assess",
    "MIN_ANPR_PLATE_PX",
    "MARGINAL_IDENTIFYING_FRACTION",
    "MIN_SAMPLES",
]

#: Plate width, in pixels, below which recognition stops being credible.
#:
#: Grounded in this estate's own two populations rather than borrowed from a
#: vendor datasheet: at a 276 px median the pipeline reads 9.4 characters and
#: 82.4% of plates exactly; at 66 px it reads 1.7 characters and none. 80 px
#: over a ten-character plate is ~8 px per character, which is the floor at
#: which a glyph has enough strokes to be distinguished at all.
MIN_ANPR_PLATE_PX = float(os.environ.get("ANPR_MIN_PLATE_PX", "80"))

#: A camera whose *median* crop is under the floor can still read the near lane,
#: or a vehicle stopped at the line. That is worth reporting separately, because
#: such a camera contributes corroborating sightings to a trace even though it
#: cannot be relied on alone.
#:
#: Deliberately **not** a second width band. Interpolating one — "45 to 80 px is
#: marginal" — would be a claim with no evidence behind it: this estate has
#: measured exactly two populations, 276 px (works) and 66 px (reads nothing),
#: and inventing a boundary between them would grade the government feeds
#: "marginal" when what they actually produce is 1.7 characters and no valid
#: plate at all.
#:
#: Nor is it a fraction of *crops* clearing the width floor, which was the first
#: attempt and was wrong for an instructive reason. Three government cameras had
#: 27-29% of crops at or above 80 px while only 4-5% of their reads were long
#: enough to narrow a search. A wide crop on those feeds is not a close vehicle;
#: it is a motion blob spanning a third of the frame — the same blob that made
#: the overlay banner readable as a plate. Width with nothing legible coming out
#: of it is not evidence of capability.
#:
#: So marginality is established by output alone: the camera must actually
#: produce reads that could narrow a search. One usable read in every seven still
#: contributes corroborating sightings to a trace; below that it is noise.
MARGINAL_IDENTIFYING_FRACTION = float(
    os.environ.get("ANPR_MARGINAL_IDENTIFYING_FRACTION", "0.15")
)

#: Below this many reads the figures are noise. Said so, rather than grading a
#: camera on three sightings — an unsupported label is worse than none, because
#: it looks like a finding.
MIN_SAMPLES = int(os.environ.get("ANPR_CAPABILITY_MIN_SAMPLES", "20"))

#: A full Indian plate is 10 characters (`GJ01AB1234`). Reading most of it is
#: what makes a read able to identify a vehicle rather than merely detect one.
FULL_PLATE_CHARS = 10


@dataclass(frozen=True)
class CameraEvidence:
    """What the pipeline recorded for one camera. All fields are measured."""

    camera_id: str
    name: str | None = None
    external_ref: str | None = None
    district: str | None = None
    sightings: int = 0
    #: Median width in pixels of the plate crop OCR was run on. None when the
    #: camera has produced no localised plate box at all.
    median_plate_px: float | None = None
    #: Mean characters in the normalised plate. The most direct expression of
    #: "could this camera resolve a registration number".
    mean_chars: float = 0.0
    #: Fraction of reads long enough to narrow a search (>= 4 characters).
    identifying_fraction: float = 0.0
    #: Fraction passing the Indian plate format check.
    format_valid_fraction: float = 0.0
    #: Fraction of plate crops at or above the ANPR width floor.
    wide_enough_fraction: float = 0.0


@dataclass(frozen=True)
class Assessment:
    """A camera's ANPR grade, with the reason stated in the same object.

    The reason travels with the grade on purpose. `situational_awareness` on its
    own reads as a judgement on the camera; "median plate crop 66 px, below the
    80 px floor" is a fact an operator can check, act on, or dispute.
    """

    grade: str
    reason: str
    #: True only for `anpr_grade`. Accuracy figures are scoped by this.
    counts_toward_accuracy: bool


#: Grades, coarsest first. Kept as constants because the API, the acceptance
#: test and the UI all key off them.
ANPR_GRADE = "anpr_grade"
MARGINAL = "marginal"
SITUATIONAL = "situational_awareness"
INSUFFICIENT = "insufficient_evidence"
SILENT = "no_reads"


def assess(evidence: CameraEvidence) -> Assessment:
    """Grade one camera from what it has actually produced.

    Ordered so that an honest "we do not know" always beats a confident label
    derived from too little data.
    """
    if evidence.sightings == 0:
        return Assessment(
            SILENT,
            "no plate reads recorded — the camera may see no traffic, may not be "
            "decoding, or may not be claimed by an ANPR worker",
            counts_toward_accuracy=False,
        )

    if evidence.sightings < MIN_SAMPLES:
        return Assessment(
            INSUFFICIENT,
            f"only {evidence.sightings} reads recorded; {MIN_SAMPLES} needed before "
            "a capability figure means anything",
            counts_toward_accuracy=False,
        )

    px = evidence.median_plate_px
    if px is None:
        return Assessment(
            INSUFFICIENT,
            "reads recorded but no plate box was ever localised, so crop size is "
            "unknown",
            counts_toward_accuracy=False,
        )

    if px >= MIN_ANPR_PLATE_PX:
        return Assessment(
            ANPR_GRADE,
            f"median plate crop {px:.0f} px across, reading {evidence.mean_chars:.1f} "
            f"of {FULL_PLATE_CHARS} characters",
            counts_toward_accuracy=True,
        )

    if evidence.identifying_fraction >= MARGINAL_IDENTIFYING_FRACTION:
        return Assessment(
            MARGINAL,
            f"median plate crop {px:.0f} px, under the {MIN_ANPR_PLATE_PX:.0f} px "
            f"floor, but {evidence.identifying_fraction * 100:.0f}% of reads are long "
            "enough to narrow a search — it reads the near lane and little else",
            counts_toward_accuracy=False,
        )

    return Assessment(
        SITUATIONAL,
        f"median plate crop {px:.0f} px across — about "
        f"{px / FULL_PLATE_CHARS:.0f} px per character. A wide-area view, not an "
        "ANPR camera; no recogniser resolves a plate at this scale",
        counts_toward_accuracy=False,
    )


def summarise(assessments: list[Assessment]) -> dict[str, int]:
    """Count each grade. The denominator for every capability-scoped figure."""
    counts = {g: 0 for g in (ANPR_GRADE, MARGINAL, SITUATIONAL, INSUFFICIENT, SILENT)}
    for a in assessments:
        counts[a.grade] = counts.get(a.grade, 0) + 1
    return counts
