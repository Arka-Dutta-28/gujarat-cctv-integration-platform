"""Cameras are positioned by place name, never by camera number.

The integration reference says camera ids and the set of available cameras can
change. A position table keyed by id does not fail loudly when they do — it
places the whole estate at somebody else's junction and the map still looks
plausible. These tests pin the property that makes that impossible.
"""

from __future__ import annotations

from services.common.gazetteer import resolve


class TestTiers:
    def test_a_named_landmark_is_placed_precisely(self) -> None:
        place = resolve("08 Majevadi Gate PTZ-2")
        assert place.precision == "landmark"
        assert place.district == "Junagadh"

    def test_a_city_name_anywhere_in_the_string_is_enough(self) -> None:
        place = resolve("Traffic junction, Rajkot city")
        assert place.precision == "city"
        assert place.district == "Rajkot"

    def test_a_district_name_falls_back_to_its_centroid(self) -> None:
        place = resolve("checkpost in Banaskantha")
        assert place.precision == "district"
        assert place.district == "Banaskantha"

    def test_an_unknown_place_is_flagged_rather_than_guessed(self) -> None:
        place = resolve("some site nobody has heard of")
        assert place.precision == "unplaced"
        assert place.placed is False

    def test_an_unplaced_camera_still_gets_a_position(self) -> None:
        """It has to: `cameras.geom` is NOT NULL, and dropping the camera would
        be worse than a flagged pin an operator can see and fix."""
        place = resolve("")
        assert -90 <= place.lat <= 90 and -180 <= place.lon <= 180


class TestMatching:
    def test_matching_is_on_whole_words(self) -> None:
        """Substring matching would put Junagadh, Punagam and Kununa in Una."""
        assert resolve("Junagadh bypass").district == "Junagadh"

    def test_the_longest_matching_key_wins(self) -> None:
        """`char chowk road` should not resolve on some shorter accidental key."""
        assert resolve("10 char chowk road").precision == "landmark"

    def test_the_hint_is_searched_too(self) -> None:
        place = resolve("Camera 4", hint="Adalaj Tollnaka")
        assert place.precision == "landmark"
        assert place.district == "Gandhinagar"

    def test_it_is_case_and_punctuation_insensitive(self) -> None:
        assert resolve("19 KHAPARIA, GANDEVI").matched == resolve("khaparia gandevi").matched


class TestRenumberingSafety:
    def test_the_same_place_under_a_different_id_lands_in_the_same_spot(self) -> None:
        """The whole point. An estate renumbered overnight still maps."""
        before = resolve("08 Majevadi Gate PTZ-2")
        after = resolve("41 Majevadi Gate PTZ-2")
        assert (before.lat, before.lon) == (after.lat, after.lon)

    def test_a_camera_number_alone_carries_no_position(self) -> None:
        """If a bare number resolved to something, the table would be keyed by
        id again through the back door."""
        assert resolve("08").precision == "unplaced"
