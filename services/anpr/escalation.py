"""Escalate a camera to the heavy models when the light ones are failing on it.

The platform's default stages are the cheap ones, and that default is backed by
measurement: the classical plate locator matched the learned detector's read
rate at one sixtieth of the cost, and Tesseract beat the available hub
recogniser by a factor of seventy-five on Indian plates. Those measurements are
what make 80,000 cameras arithmetic rather than fantasy.

But a default chosen on average is wrong somewhere. A camera at an oblique
angle, under sodium light, behind a dirty dome, or at a resolution the classical
locator's contrast assumptions do not survive, can read nothing at all while the
light path reports itself perfectly healthy: vehicles tracked, frames analysed,
zero plates. On that camera, 60x the cost of something beats 1x the cost of
nothing, and paying it is obviously right.

So the choice is not made once for the estate. It is made per camera, from that
camera's own measured read rate, and it is made in this module.

How it decides. Every finished track is one observation: did this vehicle, which
was large enough in frame to be readable, produce a plate? After a minimum
sample the read rate is compared against a floor. Below it, the camera climbs
one rung of the ladder.

Why one rung at a time. Escalating the locator and the OCR together tells you
the camera got better but not which stage was broken, and it pays for both. One
rung, then re-measure, means the record says the plate locator was the problem
on this camera, which is a fact worth having when the same question comes up on
the next thousand cameras.

Why it can give up. A camera pointed at a wall reads nothing on any model. If
the heavy path does not beat the light one by a real margin, the camera goes
back to light and stops escalating, because the alternative is an estate slowly
migrating onto its most expensive configuration in pursuit of plates that are
not there.

Why there is a budget. The heavy stages cost 15 to 60x. If a hundred cameras on
one worker escalate at once, the decoder starves and every camera gets worse,
including the ones that were fine, which is the same queueing failure already
measured on OCR concurrency. So a process may hold only a few escalations at a
time, and a camera that cannot get a slot waits rather than pushing in.

Holds no models and no database handle: the ladder is data, the decision is
arithmetic, and the caller does the swapping. That is what makes it testable
without a model runtime.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field

from services.anpr.backends import HEAVY, LIGHT

log = logging.getLogger("anpr.escalation")

__all__ = [
    "Rung",
    "LADDER",
    "EscalationPolicy",
    "EscalationBudget",
    "MIN_SAMPLE",
    "READ_RATE_FLOOR",
]

#: Tracks a camera must complete before its read rate means anything. Twenty-five
#: vehicles is enough for a rate near zero to be a property of the camera rather
#: than of a quiet minute.
MIN_SAMPLE = int(os.environ.get("ANPR_ESCALATE_MIN_SAMPLE", "25"))

#: Read rate below which the light path is judged to be failing on this camera.
#: Well under the estate-wide figure — this is meant to catch cameras reading
#: almost nothing, not cameras merely reading less than average.
READ_RATE_FLOOR = float(os.environ.get("ANPR_ESCALATE_READ_FLOOR", "0.15"))

#: How much better the heavy path must be before the escalation is kept, as a
#: multiple of the light path's rate. Below this it is paying 60x for noise.
IMPROVEMENT_FACTOR = float(os.environ.get("ANPR_ESCALATE_IMPROVEMENT", "1.5"))

#: Escalated cameras allowed at once in one worker process. Small on purpose —
#: see the budget note above.
BUDGET = int(os.environ.get("ANPR_ESCALATE_BUDGET", "2"))


@dataclass(frozen=True)
class Rung:
    """One step up the ladder: which stage gets the heavy model.

    `stage` names the pipeline attribute to replace, which is what lets the
    caller apply a rung without a branch per stage — adding a fourth analytics
    stage means adding a rung here and nothing else.
    """

    stage: str
    tier: str
    why: str


#: The ladder, cheapest useful escalation first.
#:
#: OCR before the locator, deliberately, even though the locator is the more
#: expensive stage. If the locator is finding plate-shaped regions and the OCR
#: cannot read them, swapping the locator changes nothing; and the OCR swap is
#: 4x where the locator swap is 60x. Cheapest hypothesis first is also the
#: right order diagnostically.
LADDER: tuple[Rung, ...] = (
    Rung("ocr", HEAVY, "the light OCR read almost nothing on this camera"),
    Rung("locator", HEAVY, "the classical locator found few readable plates here"),
    Rung("tracker", HEAVY, "motion proposals were not finding vehicles here"),
)


class EscalationBudget:
    """How many cameras may run heavy models in this process at once."""

    def __init__(self, size: int = BUDGET) -> None:
        self._slots = threading.Semaphore(max(0, size))
        self.size = size
        self.held = 0
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        if not self._slots.acquire(blocking=False):
            return False
        with self._lock:
            self.held += 1
        return True

    def release(self) -> None:
        with self._lock:
            if self.held <= 0:
                return
            self.held -= 1
        self._slots.release()


@dataclass
class _Window:
    """Read rate over one configuration of the pipeline."""

    tracks: int = 0
    reads: int = 0

    @property
    def rate(self) -> float:
        return self.reads / self.tracks if self.tracks else 0.0

    def reset(self) -> None:
        self.tracks = 0
        self.reads = 0


@dataclass
class EscalationPolicy:
    """Decides, for one camera, whether to move up the ladder.

    The caller feeds it finished tracks and applies whatever it returns. It
    never imports a model and never touches the pipeline itself.
    """

    camera: str
    budget: EscalationBudget | None = None
    min_sample: int = MIN_SAMPLE
    read_rate_floor: float = READ_RATE_FLOOR
    improvement_factor: float = IMPROVEMENT_FACTOR

    #: How far up the ladder this camera has climbed.
    rung: int = 0
    #: Read rate of the light path, kept so an escalation can be judged against
    #: the thing it replaced rather than against a global average.
    baseline_rate: float | None = None
    window: _Window = field(default_factory=_Window)
    #: Set once this camera has been shown to read nothing on any tier. It goes
    #: back to light and is never escalated again.
    exhausted: bool = False
    #: What was applied, in order, for the record and for the metrics.
    applied: list[str] = field(default_factory=list)
    _holds_budget: bool = False

    # --- observation ----------------------------------------------------

    def observe(self, completed: list) -> Rung | None:  # noqa: ANN001
        """Fold in a harvest of finished tracks; return a rung to apply, if any.

        A track that produced no plate is not automatically a failure — the
        vehicle may simply have been unreadable — which is why the decision
        needs a sample rather than a single miss.
        """
        for track in completed:
            self.window.tracks += 1
            if getattr(track, "result", None) is not None:
                self.window.reads += 1
        return self._decide()

    def _decide(self) -> Rung | None:
        if self.exhausted or self.window.tracks < self.min_sample:
            return None

        rate = self.window.rate

        if self.rung == 0:
            self.baseline_rate = rate
            if rate >= self.read_rate_floor:
                # Reading fine. Start a fresh window rather than accumulating
                # forever: a camera that degrades — a lens filming over, a
                # night shift — should be able to trigger later.
                self.window.reset()
                return None
            return self._climb(f"read rate {rate:.0%} over {self.window.tracks} vehicles")

        # Already escalated. Was it worth it?
        baseline = self.baseline_rate or 0.0
        improved = rate >= max(
            self.read_rate_floor, baseline * self.improvement_factor
        )
        if improved:
            log.info(
                "camera %s: %s helped — read rate %.0f%% against %.0f%% on the light path",
                self.camera, self.applied[-1], rate * 100, baseline * 100,
            )
            self.window.reset()
            return None

        if self.rung < len(LADDER):
            return self._climb(
                f"still {rate:.0%} after {self.applied[-1]} (light path was {baseline:.0%})"
            )

        return self._give_up(rate)

    def _climb(self, why: str) -> Rung | None:
        if self.budget is not None and not self._holds_budget:
            if not self.budget.acquire():
                # No slot. Not a refusal, a deferral: the window keeps filling
                # and the same decision is reached again on the next harvest.
                log.debug("camera %s: escalation deferred, budget full", self.camera)
                return None
            self._holds_budget = True

        rung = LADDER[self.rung]
        self.rung += 1
        self.applied.append(f"{rung.stage}->{rung.tier}")
        self.window.reset()
        log.warning(
            "camera %s: escalating %s to the %s model — %s (%s)",
            self.camera, rung.stage, rung.tier, why, rung.why,
        )
        return rung

    def _give_up(self, rate: float) -> Rung | None:
        """Return this camera to the light path and stop trying.

        Reported at warning level, because a camera that reads nothing on any
        model is a camera worth a human looking at — a lens problem, a view with
        no vehicles in it, or a mounting that needs moving. Silently spending
        60x on it forever hides exactly that.
        """
        self.exhausted = True
        self._release()
        log.warning(
            "camera %s: heavy models did not help (%.0f%% after %s); back to the "
            "light path and no further escalation — this camera likely needs a "
            "human to look at the view",
            self.camera, rate * 100, ", ".join(self.applied),
        )
        return Rung("__reset__", LIGHT, "no tier read this camera")

    def _release(self) -> None:
        if self._holds_budget and self.budget is not None:
            self.budget.release()
            self._holds_budget = False

    def close(self) -> None:
        """Give the budget slot back. Called when the camera thread stops."""
        self._release()

    # --- reporting ------------------------------------------------------

    @property
    def tier(self) -> str:
        return LIGHT if self.rung == 0 or self.exhausted else HEAVY

    def describe(self) -> dict[str, object]:
        """State worth putting in the metrics and on the performance page."""
        return {
            "camera": self.camera,
            "tier": self.tier,
            "rung": self.rung,
            "applied": list(self.applied),
            "baseline_read_rate": self.baseline_rate,
            "current_read_rate": self.window.rate if self.window.tracks else None,
            "exhausted": self.exhausted,
        }
