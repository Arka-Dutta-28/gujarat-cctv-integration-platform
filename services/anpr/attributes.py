"""Human-readable vehicle attributes: colour and coarse class.

Why this exists. /api/cameras/anpr-capability grades 0 of the 30 government
cameras at ANPR grade: their plate crops average 66 px across against the 80 px
the OCR needs, and a 45-minute run over all thirty produced 20 valid plates from
4,442 reads. Those cameras are not broken. They are wide-area
situational-awareness views, and they see vehicles perfectly well. They simply
cannot resolve four characters on a number plate at that distance.

Colour and coarse class survive at crop sizes where OCR is hopeless, because
they need tens of pixels rather than hundreds. So on the cameras carrying most
of the estate, this is the only description of a vehicle available, and without
it those feeds produce a coverage map and an apology.

How this differs from the re-ID embedding next door. services/anpr/reid.py
produces a 64-dimensional histogram for machine comparison: it answers "is this
the same vehicle as that one" and cannot be read, searched or explained. This
module answers "what would a person call this vehicle", such as "silver
hatchback", and that difference is the whole point:

  - an operator can search it, and cannot search a vector;
  - a report can say why two sightings were linked, which is the difference
    between evidence and a black box;
  - an alert carrying "white truck" can be dismissed by a human in one second.

They are computed from the same crop on the same frame, so the second costs
almost nothing once the first is being paid for.

What this is not. A colour name is not an identification. Several thousand white
hatchbacks pass a Gujarat highway camera in a day, and this module will call all
of them "white hatchback". Everything downstream is built on that premise:
attribute matching is the weakest tier there is, it never originates an alert,
and services/alerting/tiers.py documents the constraints that make it safe. Treat
what comes out of here as a filter, never as an answer.

Two ways to refuse. A colour is dropped when it holds too little of the bodywork
(MIN_COLOUR_CONFIDENCE) and when it is not clearly ahead of an unrelated rival
(MIN_COLOUR_MARGIN). The second catches what the first cannot: a two-tone
vehicle whose larger half is a comfortable 55% of the bodywork passes any
absolute floor worth setting, and naming it is still a coin toss reported as a
fact.

Why colour is refused rather than guessed. Vehicle colour under CCTV is
genuinely unreliable: sodium lighting turns white vans amber, headlight glare
blows out bodywork, and a wet road reflects colour onto everything above it.
Rather than emit a confident-looking name for a crop that does not support one,
describe returns no colour at all, by either of the two routes above. A sighting
with no colour is honest; a sighting that says "red" because a brake light lit up
the boot is a false lead an operator will spend time on. This follows the same
rule the rest of the platform uses for unreadable plates: say what is not known,
rather than inventing it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any

__all__ = [
    "VehicleAttributes",
    "describe",
    "colour_of",
    "COLOUR_NAMES",
    "CLASS_NAMES",
    "MIN_COLOUR_CONFIDENCE",
    "MIN_COLOUR_MARGIN",
]

#: Confidence below which no colour is reported at all. A crop whose dominant
#: colour holds less than this share of the bodywork is a crop of something
#: mixed — two-tone paint, heavy glare, a vehicle half in shadow — and naming
#: it would assert more than the pixels support.
MIN_COLOUR_CONFIDENCE = float(os.environ.get("ANPR_MIN_COLOUR_CONFIDENCE", "0.35"))

#: How far ahead of its nearest *unrelated* rival the winning colour must be,
#: as a share of bodywork. This is what refuses to name a genuinely two-tone
#: vehicle: a 50/50 white-and-blue van clears the absolute floor twice over,
#: and calling it "white" would be a coin toss reported as a fact.
#:
#: Measured against the best *unrelated* colour rather than the runner-up,
#: because the runner-up is almost always a lighting variant of the winner.
#: Real bodywork is never one value — a white car carries shadow under the
#: wheel arch and glare on the bonnet, so its pixels spread across white,
#: silver and grey. Those are the same paint under different light, and
#: requiring the winner to beat them would refuse most real vehicles while
#: catching no ambiguity at all. Blue against white is a different matter, and
#: that is the comparison this makes.
MIN_COLOUR_MARGIN = float(os.environ.get("ANPR_MIN_COLOUR_MARGIN", "0.10"))

#: Names that describe the same paint seen under different light, so that a
#: spread across them is not treated as disagreement. Two chains: the
#: achromatic ladder, which is pure brightness, and the warm end of the hue
#: circle, where a dark orange and a brown are the same panel in shade.
ADJACENT: dict[str, frozenset[str]] = {
    "white": frozenset({"silver"}),
    "silver": frozenset({"white", "grey"}),
    "grey": frozenset({"silver", "black"}),
    "black": frozenset({"grey"}),
    "red": frozenset({"brown", "orange"}),
    "orange": frozenset({"red", "brown", "yellow"}),
    "brown": frozenset({"red", "orange"}),
    "yellow": frozenset({"orange", "green"}),
    "green": frozenset({"yellow", "blue"}),
    "blue": frozenset({"green", "purple"}),
    "purple": frozenset({"blue", "red"}),
}

#: Saturation at or below which a pixel is treated as having no colour at all,
#: on OpenCV's 0-255 scale. Grey, silver, white and black vehicles are the
#: largest group on any Indian road and they are separated by *brightness*,
#: not by hue — a black car and a white car have equally meaningless hues.
ACHROMATIC_SATURATION = 60

#: Value thresholds splitting the achromatic band into black / grey / silver /
#: white, on OpenCV's 0-255 scale. Deliberately four names rather than two: an
#: operator asked to find "a silver car" will not accept "grey", and the
#: distinction costs nothing to carry.
BLACK_MAX_VALUE = 55
GREY_MAX_VALUE = 120
SILVER_MAX_VALUE = 195

#: Very dark pixels are black whatever their hue claims to be. A shadowed red
#: panel and a shadowed blue panel are both, to a camera and to a person
#: looking at the footage, black.
DARK_VALUE = 45

#: Hue bands, on OpenCV's 0-179 scale, as (name, lower, upper) with `upper`
#: exclusive. Red wraps the origin and so appears twice, which is why this is a
#: list of bands rather than a dict keyed on a range.
#:
#: These are wider than a colour scientist would draw them, on purpose. The
#: consumer is a human filtering a list, and someone searching for "a blue car"
#: means every blue including the ones a chart would call cyan or indigo. A
#: narrow band would split one vehicle's sightings across two colour names
#: between cameras, which is worse than being broad.
HUE_BANDS: tuple[tuple[str, int, int], ...] = (
    ("red", 0, 8),
    ("orange", 8, 20),
    ("yellow", 20, 33),
    ("green", 33, 78),
    ("blue", 78, 131),
    ("purple", 131, 160),
    ("red", 160, 180),
)

#: Every name this module can produce, for the API's documentation and for the
#: UI's filter list. Ordered by how common the colour is on an Indian road, so
#: a dropdown built from it does not need to sort.
COLOUR_NAMES: tuple[str, ...] = (
    "white", "silver", "grey", "black", "red", "blue",
    "brown", "orange", "yellow", "green", "purple",
)

#: The coarse classes the detector emits, mapped to what a person would say.
#: The detector's own labels are COCO's and two of them are wrong for an Indian
#: road: COCO has no `auto-rickshaw` class at all and calls one a `car` or a
#: `motorcycle` depending on the angle, and its `truck` covers everything from
#: a pickup to an articulated lorry. The mapping is therefore deliberately
#: lossy and the names are deliberately vague — `two-wheeler` rather than
#: `motorcycle`, because the platform cannot tell a scooter from a bike and
#: should not imply that it can.
CLASS_NAMES: dict[str, str] = {
    "car": "car",
    "truck": "truck",
    "bus": "bus",
    "motorcycle": "two-wheeler",
    "motorbike": "two-wheeler",
    "bicycle": "two-wheeler",
    "auto-rickshaw": "three-wheeler",
}

#: Below this many pixels of bodywork there is not enough evidence to name a
#: colour. Small enough that the wide-area government cameras still qualify —
#: a vehicle 40 px across clears it easily — and large enough to reject the
#: distant blobs that the tracker follows but nothing can describe.
MIN_BODY_PIXELS = 200


@dataclass(frozen=True)
class VehicleAttributes:
    """What a person would say this vehicle looks like.

    Every field is optional and `None` means *not established*, never
    "unremarkable". A sighting carrying no colour is one the platform declined
    to guess at, and the reports and the API render it as blank rather than
    inventing a default.
    """

    colour: str | None = None
    colour_confidence: float = 0.0
    vehicle_class: str | None = None
    #: The re-ID descriptor for the same frame (`services/anpr/reid.py`).
    #: Carried here rather than returned separately because the words and the
    #: vector are one measurement of one view: kept apart, they drifted onto
    #: two different frames and the vector stopped being computed at all for
    #: vehicles whose plate was never read.
    embedding: list[float] | None = None
    #: The pixels of the same frame, kept so the learned appearance vector
    #: (`reid.embed_many`) can be computed once, when the track finishes.
    crop: Any = None

    def with_embedding(
        self, embedding: list[float] | None, crop: Any = None
    ) -> VehicleAttributes:
        """A copy carrying the descriptor (and the crop) for the same frame."""
        return replace(self, embedding=embedding, crop=crop)

    @property
    def described(self) -> bool:
        """Whether this says anything worth storing.

        A track with neither a colour nor a class is indistinguishable from
        every other unread vehicle on the estate, and writing a row for it
        would add volume without adding evidence.
        """
        return bool(self.colour or self.vehicle_class)

    def as_text(self) -> str:
        """`silver hatchback` — for an alert line, a report cell or a log."""
        return " ".join(p for p in (self.colour, self.vehicle_class) if p) or "unknown"


def _body_band(crop: Any) -> Any | None:
    """The middle horizontal band of a vehicle box, or None if too small.

    The same reasoning as `reid.embed`: the bottom of a vehicle box is mostly
    road and shadow and the top often carries sky or the vehicle behind, while
    the middle band is bodywork. Histogramming the whole box lets the tarmac
    dominate the description of every vehicle on the estate — measured on the
    simulated farm, it made 40% of vehicles "grey".
    """
    height, width = crop.shape[:2]
    if height < 8 or width < 8:
        return None
    top, bottom = int(height * 0.25), int(height * 0.75)
    band = crop[top:bottom, :]
    return band if band.size else None


def colour_of(crop: Any) -> tuple[str | None, float]:
    """Name the dominant bodywork colour of one vehicle crop.

    Returns `(name, confidence)`, or `(None, 0.0)` when the crop cannot support
    a name. Never raises: a description is a bonus on top of a sighting, and an
    exception here would cost the sighting that carries it.
    """
    try:
        import cv2
        import numpy as np

        if crop is None or getattr(crop, "size", 0) == 0:
            return None, 0.0
        band = _body_band(crop)
        if band is None or band.size < MIN_BODY_PIXELS * 3:
            return None, 0.0

        hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
        hue, sat, val = hsv[..., 0], hsv[..., 1], hsv[..., 2]
        total = int(hue.size)
        if total < MIN_BODY_PIXELS:
            return None, 0.0

        # Every pixel votes for exactly one name, so the counts sum to `total`
        # and the winner's share is a genuine proportion rather than a score
        # that happens to be biggest.
        counts: dict[str, int] = {}

        dark = val <= DARK_VALUE
        counts["black"] = int(np.count_nonzero(dark))

        achromatic = (~dark) & (sat <= ACHROMATIC_SATURATION)
        ach_val = val[achromatic]
        if ach_val.size:
            counts["black"] += int(np.count_nonzero(ach_val <= BLACK_MAX_VALUE))
            counts["grey"] = int(
                np.count_nonzero((ach_val > BLACK_MAX_VALUE) & (ach_val <= GREY_MAX_VALUE))
            )
            counts["silver"] = int(
                np.count_nonzero((ach_val > GREY_MAX_VALUE) & (ach_val <= SILVER_MAX_VALUE))
            )
            counts["white"] = int(np.count_nonzero(ach_val > SILVER_MAX_VALUE))

        chromatic = (~dark) & (sat > ACHROMATIC_SATURATION)
        chr_hue, chr_val = hue[chromatic], val[chromatic]
        if chr_hue.size:
            for name, low, high in HUE_BANDS:
                in_band = (chr_hue >= low) & (chr_hue < high)
                if not in_band.any():
                    continue
                # Dark orange and dark red are what a person calls brown, and
                # brown vehicles are common enough on this estate that folding
                # them into "red" would send operators to the wrong cars.
                if name in ("red", "orange"):
                    band_val = chr_val[in_band]
                    brown = int(np.count_nonzero(band_val <= GREY_MAX_VALUE))
                    counts["brown"] = counts.get("brown", 0) + brown
                    counts[name] = counts.get(name, 0) + (int(in_band.sum()) - brown)
                else:
                    counts[name] = counts.get(name, 0) + int(in_band.sum())

        if not counts:
            return None, 0.0
        name, best = max(counts.items(), key=lambda kv: kv[1])
        confidence = best / total
        if best == 0 or confidence < MIN_COLOUR_CONFIDENCE:
            return None, round(confidence, 3)

        related = ADJACENT.get(name, frozenset())
        rival = max(
            (n for other, n in counts.items() if other != name and other not in related),
            default=0,
        )
        if (best - rival) / total < MIN_COLOUR_MARGIN:
            return None, round(confidence, 3)
        return name, round(confidence, 3)
    except Exception:  # noqa: BLE001 - never cost a sighting for a description
        return None, 0.0


def describe(crop: Any, detector_label: str | None = None) -> VehicleAttributes:
    """Everything this module can say about one vehicle, from one crop.

    `detector_label` is the tracker's own class for the vehicle, which is
    already known and already stored — it is mapped here rather than recomputed
    so that the class an alert quotes and the class a report prints are the
    same string.
    """
    colour, confidence = colour_of(crop)
    vehicle_class = CLASS_NAMES.get((detector_label or "").strip().lower())
    return VehicleAttributes(
        colour=colour, colour_confidence=confidence, vehicle_class=vehicle_class
    )
