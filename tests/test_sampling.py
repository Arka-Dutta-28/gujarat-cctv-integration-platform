"""Adaptive sampling and scene-condition classification.

Sampling is the lever the 80,000-camera sizing argument rests on, so its policy
is tested rather than assumed: an idle camera must cost almost nothing, and a
camera that suddenly has a vehicle in it must not be asleep.
"""

from __future__ import annotations

from services.anpr.conditions import Condition, classify
from services.anpr.sampling import AdaptiveSampler, SamplerConfig


def run(sampler: AdaptiveSampler, seconds: float, fps: float, **kwargs: float) -> int:
    """Feed `seconds` of decoded frames and count how many were analysed."""
    taken = 0
    for i in range(int(seconds * fps)):
        if sampler.should_analyse(i / fps, **kwargs):
            taken += 1
    return taken


class TestIdleCameras:
    def test_an_empty_scene_costs_almost_nothing(self) -> None:
        """Many cameras never see a vehicle. They must not cost a detector."""
        sampler = AdaptiveSampler()
        taken = run(sampler, seconds=60, fps=15)
        # 900 decoded frames; the floor rate is 0.5 Hz, so ~30 analysed.
        assert taken <= 35
        assert sampler.analysed_fraction < 0.05

    def test_the_gap_between_looks_is_bounded_even_when_idle(self) -> None:
        """A scene that changes while we are asleep must still be noticed."""
        sampler = AdaptiveSampler(config=SamplerConfig(min_hz=0.01, max_gap_s=4.0))
        assert run(sampler, seconds=20, fps=15) >= 5


class TestActivity:
    def test_motion_is_analysed_on_the_frame_it_appears(self) -> None:
        sampler = AdaptiveSampler()
        sampler.should_analyse(0.0)
        # Quiet for a while, then a vehicle enters.
        for i in range(1, 20):
            sampler.should_analyse(i / 15)
        before = sampler.frames_analysed
        assert sampler.should_analyse(20 / 15, motion=0.05) is True
        assert sampler.frames_analysed == before + 1

    def test_a_busy_scene_is_analysed_near_the_ceiling_rate(self) -> None:
        sampler = AdaptiveSampler()
        taken = run(sampler, seconds=10, fps=15, motion=0.05)
        assert 50 <= taken <= 65  # ~6 Hz

    def test_a_tracked_vehicle_keeps_the_rate_up_without_motion(self) -> None:
        """A stopped vehicle at a signal is still a vehicle worth reading."""
        sampler = AdaptiveSampler()
        assert run(sampler, seconds=10, fps=15, active_tracks=1) >= 50

    def test_the_rate_decays_rather_than_dropping_the_moment_motion_stops(self) -> None:
        """A briefly occluded vehicle must not cost us the rest of its track."""
        sampler = AdaptiveSampler()
        for i in range(30):
            sampler.should_analyse(i / 15, motion=0.05)
        during = sampler.frames_analysed
        for i in range(30, 60):  # two seconds of nothing
            sampler.should_analyse(i / 15)
        assert sampler.frames_analysed - during >= 8


class TestConditionReporting:
    def test_a_dark_scene_is_night(self) -> None:
        assert classify(mean_luma=32.0, bright_fraction=0.001) == Condition.NIGHT

    def test_a_bright_scene_is_day(self) -> None:
        assert classify(mean_luma=95.0, bright_fraction=0.001) == Condition.DAY

    def test_headlight_bloom_outranks_night(self) -> None:
        """The harder case must not be reported as merely the easier one."""
        assert classify(mean_luma=34.0, bright_fraction=0.06) == Condition.GLARE

    def test_a_bright_sky_is_not_called_glare(self) -> None:
        assert classify(mean_luma=140.0, bright_fraction=0.08) == Condition.DAY

    def test_missing_statistics_are_admitted_rather_than_guessed(self) -> None:
        assert classify(None, None) == Condition.UNKNOWN
        assert classify(50.0, None) == Condition.UNKNOWN
