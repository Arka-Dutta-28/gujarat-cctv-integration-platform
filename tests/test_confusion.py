"""The OCR confusion table is derived and merged, not typed in.

The property that matters most is the last class: the coercion map must have
gaps. A map with an entry for every character means every string of the right
length coerces into a structurally valid plate, and `format_valid` — the flag
that tells the rest of the platform to distrust a read — stops meaning
anything.
"""

from __future__ import annotations

import json

from services.common import confusion
from services.common.confusion import (
    COST_IMPLAUSIBLE,
    ConfusionModel,
    from_similarity,
    merge,
)


def _prior() -> ConfusionModel:
    return confusion._prior()  # noqa: SLF001 - the module under test


class TestPrior:
    def test_it_stands_alone_with_no_generated_table(self) -> None:
        model = _prior()
        assert model.coerce_to_digit("O") == "0"
        assert model.coerce_to_letter("5") == "S"

    def test_a_character_already_of_the_right_class_is_free(self) -> None:
        assert _prior().char_cost("4", wants_digit=True) == 0.0
        assert _prior().char_cost("A", wants_digit=False) == 0.0

    def test_an_unsupported_coercion_is_expensive_not_impossible(self) -> None:
        """A plate with one unexplained character is still read, just doubted."""
        assert _prior().char_cost("W", wants_digit=True) == COST_IMPLAUSIBLE


class TestDerivation:
    def test_similarity_becomes_a_ranked_cost(self) -> None:
        """The thing a hand table cannot supply: `O -> 0` is a surer reading
        than `J -> 1`, and the split search needs to know that."""
        model = from_similarity({"O0": 0.99, "J1": 0.60}, source="glyph")
        assert model.costs["O0"] < model.costs["J1"]

    def test_weak_pairs_are_dropped_entirely(self) -> None:
        model = from_similarity({"O0": 0.99, "WQ": 0.10}, source="glyph")
        assert "WQ" not in model.costs

    def test_the_coercion_map_is_gated_harder_than_the_cost_table(self) -> None:
        """Costs are recorded for every plausible pair; only strong pairs get
        to be the answer to 'what was this character really'."""
        scores = {"O0": 0.99, "A4": 0.95, "K4": 0.60, "M9": 0.58}
        model = from_similarity(scores, source="glyph")
        assert "K" not in model.to_digit
        assert "M" not in model.to_digit

    def test_letter_pairs_use_mutual_nearest_neighbours(self) -> None:
        """An absolute threshold either admits every letter pair or none, since
        at plate resolutions every letter resembles every other one."""
        scores = {
            "IJ": 0.95, "JI": 0.95,
            "IL": 0.94, "LI": 0.94,
            "IT": 0.93, "TI": 0.93,
            "IX": 0.80, "XI": 0.80,
        }
        model = from_similarity(scores, source="glyph")
        assert "IJ" in model.letter_pairs
        assert "IX" not in model.letter_pairs


class TestMerge:
    def test_costs_come_from_the_derived_table(self) -> None:
        overlay = from_similarity({"O0": 0.99, "0O": 0.99}, source="glyph")
        merged = merge(_prior(), overlay)
        assert merged.costs["O0"] == overlay.costs["O0"]

    def test_the_prior_keeps_its_coercion_targets(self) -> None:
        """A wrong target is a wrong plate. The glyph table ranks `Q` above `O`
        as the letter a `0` was, which is a real measurement of the wrong
        thing."""
        overlay = from_similarity({"0Q": 0.99, "Q0": 0.99}, source="glyph")
        merged = merge(_prior(), overlay)
        assert merged.to_letter["0"] == "O"

    def test_the_derived_table_fills_gaps_the_prior_left(self) -> None:
        overlay = ConfusionModel(to_digit={"W": "0"}, to_letter={"0": "O"}, source="glyph")
        merged = merge(_prior(), overlay)
        assert merged.to_digit["W"] == "0"

    def test_letter_pairs_are_the_union_of_both(self) -> None:
        overlay = ConfusionModel(
            to_digit={"O": "0"}, to_letter={"0": "O"},
            letter_pairs={"CL"}, source="glyph",
        )
        merged = merge(_prior(), overlay)
        assert "CL" in merged.letter_pairs      # from the glyphs
        assert "IJ" in merged.letter_pairs      # observed in the field

    def test_an_empirical_table_replaces_the_prior_outright(self) -> None:
        """Once the substitutions have been counted on real reads with real
        ground truth, the prior is not a floor — it is out-of-date guesswork."""
        overlay = ConfusionModel(
            to_digit={"O": "0"}, to_letter={"0": "O"},
            source="empirical", standalone=True,
        )
        merged = merge(_prior(), overlay)
        assert merged.to_digit == {"O": "0"}


class TestLoading:
    def test_a_missing_file_falls_back_to_the_prior(self, tmp_path) -> None:  # noqa: ANN001
        confusion.model.cache_clear()
        model = confusion.model(str(tmp_path / "absent.json"))
        assert model.source == "prior"
        confusion.model.cache_clear()

    def test_a_corrupt_file_falls_back_rather_than_failing(self, tmp_path) -> None:  # noqa: ANN001
        """Plate normalisation must not stop working because a generated file
        was truncated."""
        path = tmp_path / "broken.json"
        path.write_text("{not json")
        confusion.model.cache_clear()
        assert confusion.model(str(path)).source == "prior"
        confusion.model.cache_clear()

    def test_a_generated_file_is_merged_over_the_prior(self, tmp_path) -> None:  # noqa: ANN001
        path = tmp_path / "table.json"
        path.write_text(json.dumps({
            "source": "glyph",
            "to_digit": {"O": "0"},
            "to_letter": {"0": "O"},
            "letter_pairs": ["CL"],
            "costs": {"O0": 1.0},
        }))
        confusion.model.cache_clear()
        model = confusion.model(str(path))
        assert model.source == "prior+glyph"
        assert model.costs["O0"] == 1.0
        confusion.model.cache_clear()


class TestShippedTable:
    """The table checked into the repo, whatever it currently contains."""

    def test_the_map_still_has_gaps(self) -> None:
        """If every character can become a digit, nothing is ever implausible
        and `!!GARBAGE!!` normalises to a structurally valid plate."""
        model = confusion.model()
        assert len(model.to_digit) < 26

    def test_the_confusions_this_project_actually_observed_survive(self) -> None:
        """`GI 27 WR 8094` was a real misread of `GJ` on the government feeds."""
        assert confusion.model().confusable("I", "J")


class TestCoverageGuard:
    """learn_confusion refuses to replace a table with a materially thinner one.

    This exists because of a measured near-miss on 31 Aug 2026. Learning from 775
    real reads of this estate produced 1 cross-class coercion pair against the glyph
    prior's 15, and 1 cost against 234, because the pipeline reads cleanly enough
    that the residual errors are too few and too uniform to derive a table from.

    Writing that would have been silent and expensive. to_digit and to_letter are
    what positional normalisation (invariant 3) is made of, so emptying them stops
    plates being repaired at write and query time, and plates already in the index
    stop keying the same way as new ones. Nothing would have raised.
    """

    @staticmethod
    def _guard(tmp_path, old: dict, new: dict):
        import json

        from scripts.learn_confusion import _would_lose_coverage

        out = tmp_path / "ocr-confusion.json"
        out.write_text(json.dumps(old))
        return _would_lose_coverage(out, new)

    def test_gutting_the_cross_class_map_is_refused(self, tmp_path) -> None:
        refusal = self._guard(
            tmp_path,
            {"to_digit": dict.fromkeys("OISBZ", "0"), "to_letter": dict.fromkeys("015", "O"),
             "costs": {"O0": 1.0}, "letter_pairs": ["OQ"]},
            {"to_digit": {"O": "0"}, "to_letter": {}, "costs": {}, "letter_pairs": []},
        )
        assert refusal is not None
        assert "positional normalisation" in refusal

    def test_a_richer_table_is_allowed(self, tmp_path) -> None:
        assert self._guard(
            tmp_path,
            {"to_digit": {"O": "0"}, "to_letter": {}, "costs": {}, "letter_pairs": []},
            {"to_digit": dict.fromkeys("OISB", "0"), "to_letter": {"0": "O"},
             "costs": {"O0": 1.0}, "letter_pairs": ["OQ"]},
        ) is None

    def test_small_drift_between_two_honest_measurements_is_allowed(self, tmp_path) -> None:
        """Two runs over different traffic will not agree exactly, and should not
        have to. Only losing *most* of the cross-class map is the failure."""
        assert self._guard(
            tmp_path,
            {"to_digit": dict.fromkeys("OISB", "0"), "to_letter": dict.fromkeys("01", "O"),
             "costs": {"O0": 1.0, "I1": 1.0}, "letter_pairs": ["OQ"]},
            {"to_digit": dict.fromkeys("OIS", "0"), "to_letter": {"0": "O"},
             "costs": {"O0": 1.0}, "letter_pairs": []},
        ) is None

    def test_no_existing_table_is_never_refused(self, tmp_path) -> None:
        """A first run has nothing to lose, and must not be blocked."""
        from scripts.learn_confusion import _would_lose_coverage

        assert _would_lose_coverage(tmp_path / "absent.json", {"to_digit": {}}) is None

    def test_an_unreadable_existing_table_is_not_treated_as_coverage(self, tmp_path) -> None:
        """Corrupt JSON must not become an unremovable block on regenerating."""
        from scripts.learn_confusion import _would_lose_coverage

        out = tmp_path / "ocr-confusion.json"
        out.write_text("{ not json")
        assert _would_lose_coverage(out, {"to_digit": {"O": "0"}}) is None


class TestLearningFromMeasuredPairs:
    """The pairs mode, and the two scale defects found on the first real corpus.

    Both were invisible on synthetic input and obvious on 775 real reads, which
    is the argument for keeping this corpus checked in rather than generating
    one in the test.
    """

    @staticmethod
    def corpus(tmp_path, pairs: list[tuple[str, str]]):
        import json

        path = tmp_path / "pairs.json"
        path.write_text(json.dumps([{"read": r, "truth": t} for r, t in pairs]))
        return path

    def test_one_common_error_does_not_suppress_the_others(self, tmp_path) -> None:
        """The defect this pins produced a one-pair table from a 775-read corpus.

        Rates used to be rescaled against the commonest substitution, then
        compared to `MIN_SIMILARITY` — a threshold calibrated for *glyph*
        similarity, which is a different quantity. `0` read as `O` on 35% of
        zeroes pinned the peak, and `J` read as `I` — 74 times, 11% of every J
        printed — scored 0.31 after rescaling and was discarded.
        """
        from scripts.learn_confusion import learn_from_pairs

        # One dominant error, one clearly real but less frequent one.
        pairs = [("O1", "01")] * 40 + [("2I", "2J")] * 12 + [("34", "34")] * 40
        model = learn_from_pairs(self.corpus(tmp_path, pairs))
        assert model.costs, "a corpus with two real confusions produced no table"
        assert "IJ" in model.letter_pairs

    def test_a_rare_substitution_is_not_believed_on_three_examples(
        self, tmp_path
    ) -> None:
        """A rate is only as good as its denominator, and a rare character is
        exactly where a rate-based rule is easiest to fool."""
        from scripts.learn_confusion import MIN_SUPPORT, learn_from_pairs

        pairs = [("O1", "01")] * 40 + [("2Z", "22")] * (MIN_SUPPORT - 1)
        model = learn_from_pairs(self.corpus(tmp_path, pairs))
        assert "Z" not in model.to_digit
        assert model.to_digit.get("O") == "0"

    def test_a_measured_letter_confusion_is_treated_as_symmetric(
        self, tmp_path
    ) -> None:
        """Direction here is an artefact of which plates drove past, not of the
        glyphs. 679 J's went past and almost no I's, so `J` read as `I` was
        observed 74 times and the reverse never — and the mutual-neighbour test
        in `confusion.py` discarded the pair entirely."""
        from scripts.learn_confusion import learn_from_pairs

        pairs = [("2I", "2J")] * 30 + [("45", "45")] * 30
        model = learn_from_pairs(self.corpus(tmp_path, pairs))
        assert "IJ" in model.letter_pairs
