"""Attributing a misread plate to the plate it was really a corruption of.

This feeds `scripts/learn_confusion pairs`, whose output becomes the coercion
map every plate in the system is normalised through. Invariant 3 applies that
map at write time *and* at query time, so a wrong attribution here does not stay
here — it changes the join key on both sides and a plate written by one service
stops being findable by another.

Hence tests that are mostly about what attribution *refuses* to do.
"""

from __future__ import annotations

from scripts.evaluate_anpr import attribute

TRUTH = {"GJ18TR4321", "GJ05UV9972", "MP09QD3762"}


class TestAttribution:
    def test_an_exact_read_attributes_to_itself(self) -> None:
        assert attribute("GJ18TR4321", TRUTH) == "GJ18TR4321"

    def test_a_single_character_error_is_attributed(self) -> None:
        assert attribute("GJ18TR4327", TRUTH) == "GJ18TR4321"

    def test_two_character_errors_are_attributed(self) -> None:
        assert attribute("GJ08TR4327", TRUTH) == "GJ18TR4321"

    def test_three_errors_are_too_far_to_attribute(self) -> None:
        assert attribute("GJ08TR4377", TRUTH) is None

    def test_a_tie_is_refused_rather_than_guessed(self) -> None:
        """Equidistant from two plates: there is no honest answer, so give none."""
        assert attribute("GJ00TR4321", {"GJ18TR4321", "GJ28TR4321"}) is None

    def test_a_different_length_is_refused(self) -> None:
        """An insertion mis-aligns every character after it.

        The learner aligns positionally, so a length mismatch would manufacture
        substitutions that never happened for the whole tail of the plate.
        """
        assert attribute("GJ18TR43217", TRUTH) is None
        assert attribute("GJ18TR432", TRUTH) is None

    def test_unrelated_noise_is_refused(self) -> None:
        assert attribute("ZZ99ZZ9999", TRUTH) is None

    def test_an_empty_vocabulary_attributes_nothing(self) -> None:
        assert attribute("GJ18TR4321", set()) is None

    def test_an_exact_match_wins_over_a_shorter_edit_distance_tie(self) -> None:
        """A read present in the vocabulary is that plate, never a near neighbour."""
        assert attribute("GJ18TR4321", {"GJ18TR4321", "GJ18TR4322"}) == "GJ18TR4321"


class TestWhatTheLearnerNeeds:
    def test_exact_reads_are_attributable_so_denominators_stay_honest(self) -> None:
        """`learn_from_pairs` divides substitution counts by how often each
        character was *printed*. Collecting only the corrupted reads would
        inflate every rate by the pipeline's own accuracy — a table that says
        every character is confusable with every other."""
        pairs = [
            {"read": p, "truth": attribute(p, TRUTH)}
            for p in ("GJ18TR4321", "GJ18TR4321", "GJ18TR4327")
        ]
        assert all(p["truth"] is not None for p in pairs)
        assert sum(1 for p in pairs if p["read"] == p["truth"]) == 2
