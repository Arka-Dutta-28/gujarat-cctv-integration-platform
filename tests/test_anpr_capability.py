"""Per-camera ANPR capability grading.

The property under test is that the grade follows the *evidence*, and that an
absence of evidence is reported as such rather than graded. This matters more
than it looks: the estate contains 31 cameras that cannot read a plate at all,
and a platform that quietly averaged them into one accuracy figure would be
reporting a number that is true of no camera in it.
"""

from __future__ import annotations

import pytest

from services.anpr.capability import (
    ANPR_GRADE,
    INSUFFICIENT,
    MARGINAL,
    MIN_SAMPLES,
    SILENT,
    SITUATIONAL,
    CameraEvidence,
    assess,
    summarise,
)


def evidence(**kw: object) -> CameraEvidence:
    """A camera with enough reads to be gradeable, overridden per test."""
    base: dict = {
        "camera_id": "cam", "sightings": MIN_SAMPLES * 5,
        "median_plate_px": 276.0, "mean_chars": 9.4,
        "identifying_fraction": 0.98, "format_valid_fraction": 0.82,
        "wide_enough_fraction": 0.94,
    }
    base.update(kw)
    return CameraEvidence(**base)  # type: ignore[arg-type]


class TestGrading:
    def test_the_simulated_farms_measured_numbers_grade_as_anpr(self) -> None:
        """276 px / 9.4 chars — the population that scores 82.4% exact."""
        verdict = assess(evidence())
        assert verdict.grade == ANPR_GRADE
        assert verdict.counts_toward_accuracy

    def test_the_government_feeds_measured_numbers_grade_as_situational(self) -> None:
        """66 px / 1.7 chars — hundreds of reads, zero valid plates."""
        verdict = assess(
            evidence(median_plate_px=66.0, mean_chars=1.7,
                     identifying_fraction=0.05, format_valid_fraction=0.0,
                     wide_enough_fraction=0.15)
        )
        assert verdict.grade == SITUATIONAL
        assert not verdict.counts_toward_accuracy
        assert "7 px per character" in verdict.reason

    def test_a_camera_reading_only_its_near_lane_is_marginal(self) -> None:
        """Under the width floor on the median, but still producing usable reads."""
        verdict = assess(
            evidence(median_plate_px=60.0, mean_chars=5.0,
                     identifying_fraction=0.4, wide_enough_fraction=0.33)
        )
        assert verdict.grade == MARGINAL
        assert not verdict.counts_toward_accuracy

    def test_marginality_is_earned_by_output_not_by_being_close_to_the_floor(self) -> None:
        """A camera 1 px under the floor that reads nothing is not marginal.

        Proximity to a threshold is not evidence. Two cameras with the same
        median crop are graded differently when one produces usable reads and the
        other does not, which is the whole point of judging on output.
        """
        barely_under = dict(median_plate_px=79.0, mean_chars=1.6)
        assert assess(evidence(**barely_under, identifying_fraction=0.02)).grade == SITUATIONAL
        assert assess(evidence(**barely_under, identifying_fraction=0.40)).grade == MARGINAL

    def test_wide_crops_that_yield_nothing_legible_do_not_earn_marginal(self) -> None:
        """Measured: three government cameras had 28% of crops over the width
        floor and 4% identifying reads. On those feeds a wide crop is a motion
        blob spanning a third of the frame, not a close vehicle — it is the same
        blob that made the burnt-in overlay banner readable as a plate. Grading
        that "marginal" would claim a capability the camera has never shown.
        """
        verdict = assess(
            evidence(median_plate_px=39.0, mean_chars=1.4,
                     identifying_fraction=0.04, wide_enough_fraction=0.28)
        )
        assert verdict.grade == SITUATIONAL

    @pytest.mark.parametrize("px, grade", [(79.9, MARGINAL), (80.0, ANPR_GRADE)])
    def test_the_anpr_floor_is_inclusive(self, px: float, grade: str) -> None:
        assert assess(evidence(median_plate_px=px)).grade == grade


class TestHonestyAboutMissingEvidence:
    def test_a_camera_with_no_reads_is_not_graded(self) -> None:
        """Silence has three causes and the grade must not pick one."""
        verdict = assess(evidence(sightings=0, median_plate_px=None, mean_chars=0.0))
        assert verdict.grade == SILENT
        assert not verdict.counts_toward_accuracy

    def test_too_few_reads_is_reported_rather_than_graded(self) -> None:
        """An unsupported label is worse than none — it looks like a finding."""
        verdict = assess(evidence(sightings=MIN_SAMPLES - 1))
        assert verdict.grade == INSUFFICIENT
        assert str(MIN_SAMPLES - 1) in verdict.reason

    def test_a_wide_crop_on_too_few_reads_still_does_not_earn_anpr_grade(self) -> None:
        """The ordering matters: evidence sufficiency is checked before size."""
        assert assess(evidence(sightings=3, median_plate_px=400.0)).grade == INSUFFICIENT

    def test_reads_without_any_localised_plate_box_are_insufficient(self) -> None:
        """Plate size is unknown, so no size-based grade can be defended."""
        verdict = assess(evidence(median_plate_px=None))
        assert verdict.grade == INSUFFICIENT
        assert "crop size is unknown" in verdict.reason


class TestReasons:
    def test_every_grade_states_the_measurement_behind_it(self) -> None:
        """A grade an operator cannot check is an assertion, not evidence."""
        for ev in (
            evidence(),
            evidence(median_plate_px=66.0),
            evidence(median_plate_px=60.0),
            evidence(sightings=1),
            evidence(sightings=0, median_plate_px=None),
        ):
            assert len(assess(ev).reason) > 20


class TestSummary:
    def test_grades_are_counted_and_absent_grades_read_zero(self) -> None:
        counts = summarise([
            assess(evidence()),
            assess(evidence()),
            assess(evidence(median_plate_px=66.0, identifying_fraction=0.05,
                            wide_enough_fraction=0.15)),
            assess(evidence(sightings=0, median_plate_px=None)),
        ])
        assert counts[ANPR_GRADE] == 2
        assert counts[SITUATIONAL] == 1
        assert counts[SILENT] == 1
        assert counts[MARGINAL] == 0, "an absent grade must still be reported"
