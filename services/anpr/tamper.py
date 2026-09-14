"""Camera tamper detection.

A camera that has been covered, sprayed, turned to face a wall or knocked out of
alignment keeps delivering a perfectly healthy stream. Every check the platform
had before this one, the prober's reachability test, the decode rate and the
frame counter, passes throughout. The camera is up and it is useless, and on a
network of 80,000 the difference matters, because nobody walks past most of
these to notice.

What makes this cheap is that the ANPR pipeline already decodes every frame. A
tamper check is a few statistics over pixels that have already been paid for,
sampled once every few seconds rather than per frame.

Three conditions, chosen because they are what actually happens to street
cameras and because each has an unambiguous signature.

Covered or sprayed: the scene loses contrast. A lens under paint or a bag is
nearly uniform, and uniformity is the one thing a real outdoor scene never is,
day or night.

Defocused: edges disappear while brightness and contrast stay normal. The
variance of a Laplacian is the standard measure and costs one convolution.

Moved: the structure of the scene changes wholesale and stays changed. Two
refinements here were both earned by false positives rather than foreseen.

A lorry filling the frame changes the view just as completely for a few seconds,
so a single-frame comparison reports tampering every time traffic passes. It is
only tampering if the new scene persists.

Comparing brightness histograms also conflates a change of view with a change of
light. Measured on this estate, a night camera whose footage moved from
headlight glare into darkness dropped from mean luma 116 to 22 with a histogram
correlation of 0.11, and was reported as moved. Nothing had moved. So the
comparison is now made on a contrast-normalised thumbnail, a 16x16 reduction
with its own mean and standard deviation divided out, which is insensitive to
the scene getting darker or brighter and remains sensitive to it becoming a
different scene.

That fix was necessary and not sufficient, which the next measurement showed: 39
of the simulated cameras reported "moved" within an hour, and none of the 31
real ones did. The generated test clips are near-flat backgrounds with vehicle
sprites composited over them, so by construction they have almost no static
scene, and the signature tracks whatever traffic is in frame rather than the
view. A detector cannot report that a view has changed if the camera has never
demonstrated that it has a settled view.

So the check is self-calibrating, in the same way the burnt-in overlay filter
is. A camera must first prove its scene is stable, meaning most samples over a
baseline period correlate with each other, before it is eligible to be called
moved. That needs no per-camera configuration, degrades safely, since an
unstable camera is simply never reported, and is honest about the difference
between "this camera moved" and "this camera never had a fixed view to move
from".

Every threshold here is a judgement, and every one of them is wrong for some
camera: a night scene legitimately has less contrast than a daylit one, and a
PTZ camera moves because that is its job. So the detector reports a suspicion
with the measurement that produced it, and never takes an action on its own.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

__all__ = ["TamperDetector", "TamperVerdict", "COVERED", "DEFOCUSED", "MOVED"]

COVERED = "covered"
DEFOCUSED = "defocused"
MOVED = "moved"

#: Standard deviation in grey below which the scene has no contrast worth the
#: name. A covered lens sits near zero; a legitimate night scene with street
#: lighting measures well above this.
COVERED_STD = float(os.environ.get("TAMPER_COVERED_STD", "8.0"))

#: Variance of the Laplacian below which the image has no edges. Sharp daylight
#: footage measures in the hundreds; a defocused lens collapses toward zero.
DEFOCUS_LAPLACIAN = float(os.environ.get("TAMPER_DEFOCUS_LAPLACIAN", "12.0"))

#: Correlation between contrast-normalised thumbnails below which two frames
#: are "not the same view". Normalisation is what makes this a question about
#: layout rather than about lighting.
MOVED_CORRELATION = float(os.environ.get("TAMPER_MOVED_CORRELATION", "0.35"))

#: Thumbnail edge used for that comparison. Coarse on purpose: it should track
#: the arrangement of the scene, not the traffic moving through it.
SIGNATURE_PX = int(os.environ.get("TAMPER_SIGNATURE_PX", "16"))

#: How long a changed scene must persist before it is called a move rather than
#: a lorry. Sixty seconds is far longer than any vehicle occupies a frame and
#: far shorter than an operator's patience.
MOVED_PERSIST_S = float(os.environ.get("TAMPER_MOVED_PERSIST_S", "60"))

#: How often to look. Tamper is not a per-frame question, and at one sample
#: every five seconds this is free next to the decode that produced the frame.
SAMPLE_INTERVAL_S = float(os.environ.get("TAMPER_SAMPLE_INTERVAL_S", "5"))

#: Samples over which a camera must demonstrate a stable view before a "moved"
#: verdict is available for it at all.
STABILITY_SAMPLES = int(os.environ.get("TAMPER_STABILITY_SAMPLES", "12"))

#: Fraction of those samples that must match the established scene. Below this
#: the camera has no settled view — a PTZ on patrol, or footage with no static
#: structure — and no claim about it having moved is supportable.
STABILITY_FRACTION = float(os.environ.get("TAMPER_STABILITY_FRACTION", "0.7"))

#: Consecutive bad samples before reporting. A single frame can be dark or
#: blurred for honest reasons — a passing headlight, a raindrop, an I-frame
#: artefact — and reporting on one would train an operator to ignore this.
CONFIRM_SAMPLES = int(os.environ.get("TAMPER_CONFIRM_SAMPLES", "3"))


@dataclass(frozen=True)
class TamperVerdict:
    """What the detector believes, and the measurement behind it."""

    kind: str
    detail: str
    #: The measured value that triggered it, so an operator can judge the
    #: threshold rather than trust it.
    value: float


@dataclass
class TamperDetector:
    """Per-camera tamper watch over frames the pipeline already decoded.

    One instance per camera, in that camera's decode thread — not thread-safe,
    by the same reasoning as the overlay filter next to it.
    """

    sample_interval_s: float = SAMPLE_INTERVAL_S
    confirm_samples: int = CONFIRM_SAMPLES
    last_sampled_at: float = 0.0
    #: Reference histogram of the established scene, and when it was set.
    reference: Any = None
    reference_at: float = 0.0
    #: Consecutive confirming samples, per condition.
    streaks: dict[str, int] = field(default_factory=dict)
    #: When the scene first stopped matching the reference.
    changed_since: float | None = None
    #: Samples taken and samples that matched, for the stability test above.
    scene_samples: int = 0
    scene_matches: int = 0
    #: Whether the camera had a settled view *at the moment the change began*.
    #:
    #: Captured rather than evaluated live, and the distinction is the whole
    #: correctness of this check: once the view changes, every subsequent sample
    #: fails to match, so a stability ratio measured continuously would fall
    #: below its own threshold exactly when a genuine move was being confirmed —
    #: disqualifying the camera precisely when it had something to report.
    stable_when_changed: bool = False
    reported: set[str] = field(default_factory=set)

    def observe(self, frame: Any, now: float) -> TamperVerdict | None:
        """Sample this frame if due; return a verdict the first time one holds.

        Returns a verdict once per condition until it clears, because an alert that
        repeats every five seconds is an alert an operator silences.
        """
        if now - self.last_sampled_at < self.sample_interval_s:
            return None
        self.last_sampled_at = now

        try:
            import cv2
        except Exception:  # noqa: BLE001 - no OpenCV, no tamper detection
            return None

        try:
            grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        except Exception:  # noqa: BLE001 - a malformed frame is not a tamper
            return None

        verdict = (
            self._check_covered(grey)
            or self._check_defocus(grey, cv2)
            or self._check_moved(grey, cv2, now)
        )
        if verdict is None:
            return None
        if verdict.kind in self.reported:
            return None
        self.reported.add(verdict.kind)
        return verdict

    # --- conditions ---

    def _check_covered(self, grey: Any) -> TamperVerdict | None:
        std = float(grey.std())
        if not self._streak(COVERED, std < COVERED_STD):
            return None
        return TamperVerdict(
            COVERED,
            f"the scene has almost no contrast (grey std {std:.1f}, below "
            f"{COVERED_STD:.0f}) across {self.confirm_samples} samples — the lens "
            "may be covered, sprayed or facing a blank surface",
            std,
        )

    def _check_defocus(self, grey: Any, cv2: Any) -> TamperVerdict | None:
        sharpness = float(cv2.Laplacian(grey, cv2.CV_64F).var())
        if not self._streak(DEFOCUSED, sharpness < DEFOCUS_LAPLACIAN):
            return None
        return TamperVerdict(
            DEFOCUSED,
            f"the image has almost no edges (Laplacian variance {sharpness:.1f}, "
            f"below {DEFOCUS_LAPLACIAN:.0f}) while still carrying contrast — the "
            "lens may have been defocused",
            sharpness,
        )

    def _check_moved(self, grey: Any, cv2: Any, now: float) -> TamperVerdict | None:
        signature = _signature(grey, cv2)
        if signature is None:
            return None

        if self.reference is None:
            self.reference, self.reference_at = signature, now
            return None

        correlation = _correlate(self.reference, signature)
        self.scene_samples += 1
        if correlation >= MOVED_CORRELATION:
            self.scene_matches += 1
            # Back to the established view: whatever was in front of the camera
            # has gone. Drift with it, so slow legitimate change — dusk, a
            # season — never accumulates into a false report.
            self.changed_since = None
            self.reference = signature
            self.reference_at = now
            return None

        # The scene has changed. A lorry does this too, so only its persistence
        # distinguishes tampering from traffic.
        if self.changed_since is None:
            self.changed_since = now
            self.stable_when_changed = self.has_stable_scene
            return None
        if now - self.changed_since < MOVED_PERSIST_S:
            return None

        # And it is only a claim worth making about a camera that had a settled
        # view in the first place. Measured: without this, 39 of the simulated
        # cameras reported "moved" in an hour and none of the real ones did.
        if not self.stable_when_changed:
            return None

        return TamperVerdict(
            MOVED,
            f"the view has been structurally different for "
            f"{now - self.changed_since:.0f}s (correlation {correlation:.2f} "
            "against the established scene, with brightness normalised out) — "
            "the camera may have been moved or obstructed",
            correlation,
        )

    @property
    def has_stable_scene(self) -> bool:
        """Whether this camera has shown a fixed view worth comparing against."""
        if self.scene_samples < STABILITY_SAMPLES:
            return False
        return (self.scene_matches / self.scene_samples) >= STABILITY_FRACTION

    def _streak(self, kind: str, condition: bool) -> bool:
        """Track consecutive confirmations, and clear a condition that recovers."""
        if not condition:
            self.streaks[kind] = 0
            self.reported.discard(kind)
            return False
        self.streaks[kind] = self.streaks.get(kind, 0) + 1
        return self.streaks[kind] >= self.confirm_samples


def _signature(grey: Any, cv2: Any) -> Any:
    """A contrast-normalised thumbnail: what the scene looks like, not how lit.

    Subtracting the mean and dividing by the standard deviation removes exactly
    the two things that change when the sun sets or a floodlight comes on, and
    keeps the arrangement of light and dark that changes when a camera is turned.
    """
    import numpy as np

    small = cv2.resize(grey, (SIGNATURE_PX, SIGNATURE_PX), interpolation=cv2.INTER_AREA)
    small = small.astype(np.float32)
    spread = float(small.std())
    if spread < 1e-3:
        # A featureless frame has no structure to compare. The covered check is
        # the one that should speak about it, not this one.
        return None
    return (small - float(small.mean())) / spread


def _correlate(a: Any, b: Any) -> float:
    """Pearson correlation between two normalised signatures."""
    import numpy as np

    return float(np.mean(a * b))
