"""Regression tests for the fast-plate-ocr return-shape adapter.

These exist because the failure they cover was silent. `fast-plate-ocr` moved
from returning `(texts, confidences)` to returning `list[PlatePrediction]`.
The old unpacking code did not raise on the new shape — it stringified the
dataclass — so the pipeline happily wrote 25,157 sightings whose plate was
`PlatePrediction(plate='2J2ZN4469', char_probs=array([...]))`, passed every
acceptance check, and looked like a working ANPR system.

The lesson encoded here is that an adapter over a third-party return shape must
*refuse* what it does not recognise. A read we cannot parse is a fact worth
recording; a plate invented from a repr is worse than no plate at all.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from services.anpr.backends.plates import _unpack


@dataclass
class FakePrediction:
    """Same duck type as fast_plate_ocr.PlatePrediction."""

    plate: str
    char_probs: list[float] | None = None
    region: str | None = None
    region_prob: float | None = None


def test_reads_the_current_list_of_predictions_shape() -> None:
    result = [FakePrediction(plate="GJ01AB1234", char_probs=[0.9, 0.8, 0.95, 0.7])]
    text, confidence = _unpack(result)
    assert text == "GJ01AB1234"
    # The weakest character, not the mean.
    assert confidence == pytest.approx(0.7)


def test_never_stringifies_the_dataclass() -> None:
    """The exact bug: the repr must never reach the plate field."""
    text, _ = _unpack([FakePrediction(plate="2J2ZN4469", char_probs=[0.8, 0.5])])
    assert "PlatePrediction" not in text
    assert "char_probs" not in text
    assert text == "2J2ZN4469"


def test_a_bare_prediction_is_accepted_as_well_as_a_list() -> None:
    text, confidence = _unpack(FakePrediction(plate="GJ18XY0007", char_probs=[0.99, 0.42]))
    assert text == "GJ18XY0007"
    assert confidence == pytest.approx(0.42)


def test_missing_probabilities_fall_back_to_a_neutral_confidence() -> None:
    text, confidence = _unpack([FakePrediction(plate="GJ05CD9999")])
    assert text == "GJ05CD9999"
    assert confidence == 0.5


def test_legacy_tuple_shape_still_works() -> None:
    text, confidence = _unpack((["GJ27EF4321"], [[0.9, 0.6, 0.99]]))
    assert text == "GJ27EF4321"
    assert confidence == pytest.approx(0.6)


def test_legacy_bare_string_shape_still_works() -> None:
    assert _unpack("GJ01AB1234") == ("GJ01AB1234", 0.5)
    assert _unpack(["GJ01AB1234"]) == ("GJ01AB1234", 0.5)


def test_an_unrecognised_shape_is_refused_not_coerced() -> None:
    """Silence is the danger here, so an unknown shape must yield no read."""
    text, confidence = _unpack([object()])
    assert text == ""
    assert confidence == 0.0


def test_empty_results_yield_no_read() -> None:
    assert _unpack([]) == ("", 0.0)


def test_confidence_is_clamped_into_the_unit_interval() -> None:
    _, confidence = _unpack([FakePrediction(plate="GJ01AB1234", char_probs=[-0.2, 1.7])])
    assert 0.0 <= confidence <= 1.0
