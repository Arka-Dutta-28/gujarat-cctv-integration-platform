"""Scene condition, so accuracy can be reported per condition.

The honesty convention requires reporting confidence honestly rather than claiming an
accuracy figure that cannot be defended, and the field observations make one
number indefensible: three of the four real feeds seen are night scenes, two
with severe headlight bloom that swallows whole vehicles. An ANPR accuracy of
"92%" averaged across that is not a measurement, it is an average of two
different problems.

The classification is deliberately crude — mean brightness and how much of the
frame is blown out. It does not need to be clever; it needs to split the
results into groups an evaluator can read, and to be computable from statistics
the decoder already has.

Glare outranks night: a night frame with headlight bloom is the harder case and
reporting it as merely "night" would flatter the numbers.
"""

from __future__ import annotations

__all__ = ["Condition", "classify", "NIGHT_MEAN_LUMA", "GLARE_BRIGHT_FRACTION"]


class Condition:
    DAY = "day"
    NIGHT = "night"
    GLARE = "glare"
    UNKNOWN = "unknown"


#: Mean luma (0-255) below which a scene is night. Set from the generated
#: night clips, which measure ~32, against day clips at ~59.
NIGHT_MEAN_LUMA = 45.0

#: Fraction of near-saturated pixels that means headlight bloom rather than
#: a bright sky. Bloom is concentrated and blows out completely; daylight is
#: bright but rarely clipped across this much of the frame.
GLARE_BRIGHT_FRACTION = 0.02


def classify(mean_luma: float | None, bright_fraction: float | None) -> str:
    """Bucket one frame's brightness statistics into a reporting condition."""
    if mean_luma is None or bright_fraction is None:
        return Condition.UNKNOWN
    if bright_fraction >= GLARE_BRIGHT_FRACTION and mean_luma < NIGHT_MEAN_LUMA * 1.6:
        # Blown-out regions in an otherwise dark frame: headlights, not daylight.
        return Condition.GLARE
    if mean_luma < NIGHT_MEAN_LUMA:
        return Condition.NIGHT
    return Condition.DAY
