"""Adaptive frame sampling.

The single biggest lever in the 80,000-camera argument. Decoding every frame of
every stream and running a detector on it is arithmetic that does not close:
at 15 fps, 80,000 cameras is 1.2 million detector invocations a second. It is
also mostly wasted — the field observations are explicit that many cameras look
at a market stall or an empty lane and will not see a vehicle for hours.

So the pipeline decodes continuously (cheap, and needed to keep the connection
live) but *analyses* on a rate that follows the scene:

- an idle camera is analysed at a floor rate, just often enough to notice that
  something has started happening;
- motion pulls it straight up to the ceiling rate, on the frame the motion is
  seen rather than at the next scheduled tick — a vehicle entering the frame is
  precisely the moment not to be asleep;
- activity holds it there, and it decays back down over a few seconds rather
  than immediately, because a vehicle briefly occluded is still a vehicle.

Deliberately pure: it takes numbers and returns a decision, so the policy can
be tested without decoding anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["SamplerConfig", "AdaptiveSampler"]


@dataclass(frozen=True)
class SamplerConfig:
    #: Analyses per second when the scene is busy. Above the rate at which a
    #: vehicle crosses a typical field of view, so no vehicle is missed.
    max_hz: float = 6.0
    #: Analyses per second when nothing is happening.
    min_hz: float = 0.5
    #: Motion above this fraction of the frame counts as activity.
    motion_threshold: float = 0.004
    #: How long activity keeps the rate up after the last motion or track.
    hold_s: float = 3.0
    #: Never go longer than this without looking, whatever the scene says.
    max_gap_s: float = 4.0


@dataclass
class AdaptiveSampler:
    """Decides which decoded frames are worth analysing."""

    config: SamplerConfig = field(default_factory=SamplerConfig)
    last_analysed_at: float | None = None
    busy_until: float = 0.0
    #: Counters, so the saving can be reported rather than asserted.
    frames_seen: int = 0
    frames_analysed: int = 0

    def should_analyse(
        self, now: float, motion: float = 0.0, active_tracks: int = 0
    ) -> bool:
        """True if this decoded frame should go through the detector.

        `now` is a monotonic timestamp, `motion` the fraction of the frame that
        changed, `active_tracks` how many vehicles are currently being tracked.
        """
        self.frames_seen += 1
        cfg = self.config

        busy = motion >= cfg.motion_threshold or active_tracks > 0
        if busy:
            self.busy_until = now + cfg.hold_s

        if self.last_analysed_at is None:
            return self._take(now)

        gap = now - self.last_analysed_at

        # A scene that has just come alive is analysed at once. Waiting for the
        # next tick is how a vehicle crosses the frame unseen.
        if busy and self.busy_until - cfg.hold_s == now and gap >= 1.0 / cfg.max_hz:
            return self._take(now)

        target_hz = cfg.max_hz if now < self.busy_until else cfg.min_hz
        if gap >= 1.0 / target_hz or gap >= cfg.max_gap_s:
            return self._take(now)
        return False

    def _take(self, now: float) -> bool:
        self.last_analysed_at = now
        self.frames_analysed += 1
        return True

    @property
    def analysed_fraction(self) -> float:
        """Share of decoded frames that reached the detector."""
        return self.frames_analysed / self.frames_seen if self.frames_seen else 0.0
