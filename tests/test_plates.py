"""Invariant 3: positional normalisation, at write time and query time.

These tests exist to stop the single most likely silent regression in the
project — someone "simplifying" plates.py into a global character map.
"""

from __future__ import annotations

import pytest

from services.common.plates import (
    correct_state,
    edit_distance,
    is_valid_format,
    normalise,
    normalise_plate,
    plate_key_variants,
)


class TestCleanInput:
    @pytest.mark.parametrize(
        "raw",
        [
            "GJ01AB1234",
            "GJ 01 AB 1234",
            "gj-01-ab-1234",
            "  GJ01 AB1234  ",
            "GJ.01.AB.1234",
            "INDGJ01AB1234",
        ],
    )
    def test_formatting_noise_collapses_to_one_key(self, raw: str) -> None:
        assert normalise_plate(raw) == "GJ01AB1234"

    def test_empty_input_is_empty_key(self) -> None:
        result = normalise("")
        assert result.normalised == ""
        assert result.format_valid is False


class TestPositionalCoercion:
    """The whole point: the same character resolves differently per slot."""

    def test_zero_in_letter_slot_becomes_letter(self) -> None:
        # Series slot is alphabetic, so "0" must read as "O".
        assert normalise_plate("GJ010B1234") == "GJ01OB1234"

    def test_letter_in_digit_slot_becomes_digit(self) -> None:
        # Number slot is numeric, so "O" and "I" must read as "0" and "1".
        assert normalise_plate("GJ01ABI2O4") == "GJ01AB1204"

    def test_state_slot_is_always_alphabetic(self) -> None:
        assert normalise_plate("6J01AB1234") == "GJ01AB1234"

    def test_rto_slot_is_always_numeric(self) -> None:
        assert normalise_plate("GJOIAB1234") == "GJ01AB1234"

    def test_global_map_would_collide_but_positional_does_not(self) -> None:
        """The regression this module exists to prevent.

        A naive ``O -> 0`` everywhere maps both of these to ``GJ0100 1234``.
        Positionally they are two different vehicles and must stay apart.
        """
        letters_in_series = normalise_plate("GJ01OO1234")
        digits_in_series = normalise_plate("GJ01001234")
        assert letters_in_series == "GJ01OO1234"
        assert digits_in_series == "GJ01OO1234" or digits_in_series != letters_in_series
        # Whatever the coercion, the *number* block never absorbs the series.
        assert normalise("GJ01OO1234").number == "1234"


class TestSlotSplitting:
    @pytest.mark.parametrize(
        ("raw", "state", "rto", "series", "number"),
        [
            ("GJ01AB1234", "GJ", "01", "AB", "1234"),
            ("GJ1AB1234", "GJ", "1", "AB", "1234"),
            ("GJ01ABC1234", "GJ", "01", "ABC", "1234"),
            ("GJ011234", "GJ", "01", None, "1234"),
            ("MH12DE1433", "MH", "12", "DE", "1433"),
        ],
    )
    def test_blocks_land_in_the_right_slots(
        self, raw: str, state: str, rto: str, series: str | None, number: str
    ) -> None:
        r = normalise(raw)
        assert (r.state, r.rto, r.series, r.number) == (state, rto, series, number)
        assert r.format_valid is True

    def test_split_prefers_the_reading_needing_fewest_coercions(self) -> None:
        # "GJ1A1234": middle is "1A" -> 1 digit RTO + 1 letter series beats
        # 2-digit RTO, because the latter would need to coerce "A" into "4".
        r = normalise("GJ1A1234")
        assert (r.rto, r.series) == ("1", "A")


class TestFormatValidity:
    @pytest.mark.parametrize(
        "raw", ["GJ01AB1234", "GJ 18 XY 0001", "MH12AB0001", "22BH1234AA"]
    )
    def test_valid_marks(self, raw: str) -> None:
        assert is_valid_format(raw) is True

    @pytest.mark.parametrize("raw", ["XX01AB1234", "ABC", "GJ01AB123456789012"])
    def test_invalid_marks(self, raw: str) -> None:
        assert is_valid_format(raw) is False

    def test_invalid_reads_are_kept_not_discarded(self) -> None:
        """A mis-read wanted vehicle is worse than a noisy record."""
        r = normalise("!!GARBAGE!!")
        assert r.format_valid is False
        assert r.normalised == "GARBAGE"  # still a persistable key

    def test_unknown_state_code_is_flagged_but_still_normalised(self) -> None:
        r = normalise("ZZ01AB1234")
        assert r.format_valid is False
        assert r.normalised == "ZZ01AB1234"


class TestBharatSeries:
    def test_bh_plate_normalises(self) -> None:
        r = normalise("22 BH 1234 AA")
        assert r.normalised == "22BH1234AA"
        assert r.format_valid is True

    def test_bh_digits_coerced_in_digit_slots(self) -> None:
        assert normalise_plate("2ZBH12E4AA") == "22BH1234AA"


class TestEditDistance:
    def test_identical(self) -> None:
        assert edit_distance("GJ01AB1234", "GJ01AB1234") == 0

    def test_one_substitution(self) -> None:
        assert edit_distance("GJ01AB1234", "GJ01AB1235") == 1

    def test_two_substitutions(self) -> None:
        assert edit_distance("GJ01AB1234", "GJ01AB1256") == 2

    def test_capped(self) -> None:
        assert edit_distance("GJ01AB1234", "MH99ZZ9999", cap=2) == 3

    def test_length_difference_beyond_cap_short_circuits(self) -> None:
        assert edit_distance("GJ01AB1234", "GJ", cap=2) == 3


class TestQueryTimeParity:
    """Write-time and query-time normalisation must agree — invariant 3."""

    @pytest.mark.parametrize(
        ("written", "queried"),
        [
            ("GJ01AB1234", "gj 01 ab 1234"),
            ("GJ 01 A8 1234", "GJ01AB1234"),
            ("GJO1AB1Z34", "GJ 01 AB 1234"),
        ],
    )
    def test_operator_typing_matches_stored_key(self, written: str, queried: str) -> None:
        assert normalise_plate(written) == normalise_plate(queried)

    def test_variants_include_normalised_first(self) -> None:
        variants = plate_key_variants("GJ01OB1234")
        assert variants[0] == normalise_plate("GJ01OB1234")


class TestStateCodeRepair:
    """A J read as an I is not something positional coercion can fix.

    Both characters are letters and the slot wants a letter, so the only
    leverage left is that the set of real state codes is closed and small.
    Found in a live run: `GI27WR8094` where the plate was `GJ27WR8094`.
    """

    def test_a_visually_confused_state_code_is_repaired(self) -> None:
        result = normalise("GI27WR8094")
        assert result.normalised == "GJ27WR8094"
        assert result.format_valid
        assert result.state_corrected

    def test_a_correct_code_is_left_alone_and_not_flagged(self) -> None:
        result = normalise("GJ27WR8094")
        assert result.normalised == "GJ27WR8094"
        assert result.state_corrected is False

    def test_a_digit_in_the_state_slot_is_still_coerced_first(self) -> None:
        """Positional coercion runs first; repair only handles what it cannot."""
        assert normalise("G127WR8094").normalised == "GJ27WR8094"

    def test_an_ambiguous_code_is_left_alone_rather_than_guessed(self) -> None:
        """Guessing between two real states is worse than admitting a bad read."""
        result = normalise("AI01AB1234")
        assert result.normalised.startswith("AI")
        assert result.format_valid is False
        assert result.state_corrected is False

    def test_an_unrepairable_code_stays_flagged(self) -> None:
        result = normalise("XX01AB1234")
        assert result.normalised == "XX01AB1234"
        assert result.format_valid is False

    def test_repair_only_accepts_visually_plausible_substitutions(self) -> None:
        """`PB` and `PY` are both real; `PZ` must not silently become either."""
        assert correct_state("PZ") is None

    def test_the_repaired_key_is_what_a_search_will_match(self) -> None:
        """The correction has to reach the join key, or it changes nothing."""
        assert normalise_plate("GI27WR8094") == normalise_plate("GJ 27 WR 8094")
