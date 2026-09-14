"""Heavy models as a measured, budgeted fallback — not as a default.

The platform's cheap stages are the default because they were measured as good
enough on average. This is what happens on the cameras where they are not.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.anpr.backends import HEAVY, LIGHT
from services.anpr.escalation import LADDER, EscalationBudget, EscalationPolicy


def _tracks(count: int, reads: int) -> list:
    """`count` finished tracks, `reads` of which produced a plate."""
    return [
        SimpleNamespace(result=SimpleNamespace(plate="X") if i < reads else None)
        for i in range(count)
    ]


class TestDoesNothingWhenThingsAreFine:
    def test_a_camera_reading_well_is_left_alone(self) -> None:
        policy = EscalationPolicy(camera="cam-1", min_sample=10)
        assert policy.observe(_tracks(10, 8)) is None
        assert policy.tier == LIGHT

    def test_it_will_not_decide_on_too_small_a_sample(self) -> None:
        """One quiet minute is not evidence about a camera."""
        policy = EscalationPolicy(camera="cam-1", min_sample=25)
        assert policy.observe(_tracks(5, 0)) is None
        assert policy.rung == 0

    def test_a_camera_that_degrades_later_can_still_trigger(self) -> None:
        policy = EscalationPolicy(camera="cam-1", min_sample=10)
        policy.observe(_tracks(10, 9))       # healthy window
        assert policy.observe(_tracks(10, 0)) is not None


class TestEscalation:
    def test_a_camera_reading_nothing_moves_up_the_ladder(self) -> None:
        policy = EscalationPolicy(camera="cam-1", min_sample=10)
        rung = policy.observe(_tracks(10, 0))
        assert rung is not None
        assert rung.tier == HEAVY
        assert policy.tier == HEAVY

    def test_it_tries_the_cheapest_hypothesis_first(self) -> None:
        """Swapping the 60x locator before the 4x OCR pays more to learn less."""
        assert LADDER[0].stage == "ocr"
        policy = EscalationPolicy(camera="cam-1", min_sample=10)
        assert policy.observe(_tracks(10, 0)).stage == "ocr"

    def test_one_rung_at_a_time_so_the_record_says_which_stage_it_was(self) -> None:
        policy = EscalationPolicy(camera="cam-1", min_sample=10)
        policy.observe(_tracks(10, 0))
        assert policy.applied == ["ocr->heavy"]
        policy.observe(_tracks(10, 0))
        assert policy.applied == ["ocr->heavy", "locator->heavy"]

    def test_an_escalation_that_helped_is_kept(self) -> None:
        policy = EscalationPolicy(camera="cam-1", min_sample=10)
        policy.observe(_tracks(10, 0))
        assert policy.observe(_tracks(10, 7)) is None
        assert policy.rung == 1
        assert policy.tier == HEAVY


class TestGivingUp:
    def test_a_camera_no_model_can_read_returns_to_the_light_path(self) -> None:
        """A camera pointed at a wall reads nothing on any tier, and paying 60x
        for that for ever is how an estate migrates onto its most expensive
        configuration in pursuit of plates that are not there."""
        policy = EscalationPolicy(camera="cam-1", min_sample=10)
        for _ in range(len(LADDER)):
            policy.observe(_tracks(10, 0))
        reset = policy.observe(_tracks(10, 0))
        assert reset is not None
        assert reset.stage == "__reset__"
        assert reset.tier == LIGHT
        assert policy.exhausted
        assert policy.tier == LIGHT

    def test_it_never_escalates_again_once_exhausted(self) -> None:
        policy = EscalationPolicy(camera="cam-1", min_sample=10)
        for _ in range(len(LADDER) + 1):
            policy.observe(_tracks(10, 0))
        assert policy.observe(_tracks(50, 0)) is None

    def test_the_budget_slot_is_returned_when_it_gives_up(self) -> None:
        budget = EscalationBudget(size=1)
        policy = EscalationPolicy(camera="cam-1", min_sample=10, budget=budget)
        for _ in range(len(LADDER) + 1):
            policy.observe(_tracks(10, 0))
        assert budget.held == 0


class TestBudget:
    def test_only_so_many_cameras_may_run_heavy_at_once(self) -> None:
        """A hundred cameras escalating together starves every decoder on the
        box, including the ones that were reading perfectly well."""
        budget = EscalationBudget(size=2)
        policies = [
            EscalationPolicy(camera=f"cam-{i}", min_sample=10, budget=budget)
            for i in range(5)
        ]
        escalated = [p for p in policies if p.observe(_tracks(10, 0)) is not None]
        assert len(escalated) == 2

    def test_a_deferred_camera_tries_again_rather_than_being_refused(self) -> None:
        budget = EscalationBudget(size=1)
        first = EscalationPolicy(camera="cam-1", min_sample=10, budget=budget)
        second = EscalationPolicy(camera="cam-2", min_sample=10, budget=budget)
        first.observe(_tracks(10, 0))
        assert second.observe(_tracks(10, 0)) is None
        first.close()
        assert second.observe(_tracks(10, 0)) is not None

    def test_closing_a_camera_frees_its_slot(self) -> None:
        budget = EscalationBudget(size=1)
        policy = EscalationPolicy(camera="cam-1", min_sample=10, budget=budget)
        policy.observe(_tracks(10, 0))
        assert budget.held == 1
        policy.close()
        assert budget.held == 0


class TestHeavyBackendsFailAtBuildTime:
    """A heavy backend that cannot run must refuse when it is *built*.

    The heavy backends import their libraries lazily, inside the inference call,
    so this module stays importable on the lean image. Correct — but it meant
    constructing one succeeded and the failure landed on the first *frame*.

    Escalation guards construction and rolls a camera back to the light path
    when a model will not load. A failure deferred to first use walks straight
    past that guard. Measured 31 Aug 2026: one camera escalating to YOLO on an
    image without `ultralytics` logged `frame failed` on every frame — for that
    camera and others — degrading an estate that had been working.
    """

    def test_require_refuses_a_missing_module(self) -> None:
        from services.anpr.backends import require

        with pytest.raises(ModuleNotFoundError) as caught:
            require("definitely_not_installed_xyz", "test recogniser")
        message = str(caught.value)
        assert "definitely_not_installed_xyz" in message
        # It must say what to do, not merely what is missing.
        assert "Dockerfile.gpu" in message
        assert "test recogniser" in message

    def test_require_passes_for_a_module_that_is_present(self) -> None:
        from services.anpr.backends import require

        require("json", "a backend that needs json")  # must not raise

    def test_the_light_path_never_calls_require(self) -> None:
        """The lean image is the default, and it must not depend on any of this."""
        from services.anpr.backends import build_locator, build_ocr, build_tracker

        # These are what the lean image runs; none may raise on an image with
        # neither ultralytics nor onnxruntime installed.
        assert build_tracker("motion") is not None
        assert build_locator("classical") is not None
        assert build_ocr("tesseract") is not None
