"""Naming what a vehicle looks like, and refusing to when the crop cannot say.

The interesting assertions here are the refusals. A colour name is the only
description the 30 government cameras can produce, which makes it tempting to
produce one for every crop — and a confident `red` taken from a brake light is
a false lead an operator spends real time on, in a way that a blank field never
is. Most of these tests pin the conditions under which this module declines.
"""

from __future__ import annotations

import numpy as np
import pytest

from services.anpr import attributes
from services.anpr.attributes import VehicleAttributes, colour_of, describe


def block(bgr: tuple[int, int, int], size: int = 60) -> np.ndarray:
    """A plain crop of one colour, big enough to clear MIN_BODY_PIXELS."""
    crop = np.zeros((size, size, 3), dtype=np.uint8)
    crop[:, :] = bgr
    return crop


def two_tone(top: tuple[int, int, int], bottom: tuple[int, int, int]) -> np.ndarray:
    crop = np.zeros((60, 60, 3), dtype=np.uint8)
    crop[:30, :] = top
    crop[30:, :] = bottom
    return crop


class TestNamingColours:
    @pytest.mark.parametrize(
        ("bgr", "expected"),
        [
            ((255, 255, 255), "white"),
            ((160, 160, 160), "silver"),
            ((90, 90, 90), "grey"),
            ((10, 10, 10), "black"),
            ((40, 40, 200), "red"),
            ((200, 60, 40), "blue"),
            ((40, 170, 40), "green"),
            ((40, 220, 230), "yellow"),
        ],
    )
    def test_a_plain_vehicle_is_named(self, bgr: tuple[int, int, int], expected: str) -> None:
        name, confidence = colour_of(block(bgr))
        assert name == expected
        assert confidence > 0.9

    def test_the_achromatic_range_is_split_by_brightness_not_hue(self) -> None:
        """White, silver, grey and black differ only in value, and must not merge.

        They are the largest group on any Indian road. Folding them into one
        "greyscale" bucket would make the attribute tier useless precisely
        where most vehicles are.
        """
        names = {colour_of(block((v, v, v)))[0] for v in (255, 160, 90, 10)}
        assert names == {"white", "silver", "grey", "black"}

    def test_a_dark_red_is_brown_rather_than_red(self) -> None:
        """Brown vehicles are common enough that folding them into red would
        send an operator to the wrong cars."""
        assert colour_of(block((20, 30, 90)))[0] == "brown"

    def test_a_very_dark_pixel_is_black_whatever_its_hue_claims(self) -> None:
        """A shadowed red panel and a shadowed blue panel both look black —
        to the camera and to anyone watching the footage."""
        assert colour_of(block((0, 0, 30)))[0] == "black"
        assert colour_of(block((30, 0, 0)))[0] == "black"


class TestRefusing:
    def test_a_two_tone_crop_is_refused_rather_than_split(self) -> None:
        """A 50/50 white-and-blue van clears the absolute floor twice over.
        It is refused on the *margin* instead: neither colour is clearly ahead
        of the other, so naming one is a coin toss reported as a fact."""
        name, confidence = colour_of(two_tone((255, 255, 255), (40, 40, 200)))
        assert name is None
        assert confidence > attributes.MIN_COLOUR_CONFIDENCE  # the floor was not the reason

    def test_the_margin_is_measured_against_unrelated_colours_only(self, monkeypatch) -> None:
        """Raising the margin above what any real vehicle can clear refuses
        everything two-toned, and still keeps a plain vehicle."""
        monkeypatch.setattr(attributes, "MIN_COLOUR_MARGIN", 0.95)
        assert colour_of(block((40, 40, 200)))[0] == "red"
        assert colour_of(two_tone((255, 255, 255), (40, 40, 200)))[0] is None

    def test_a_crop_too_small_to_describe_is_refused(self) -> None:
        assert colour_of(np.zeros((4, 4, 3), dtype=np.uint8)) == (None, 0.0)

    def test_an_empty_crop_is_refused(self) -> None:
        assert colour_of(np.zeros((0, 0, 3), dtype=np.uint8)) == (None, 0.0)

    def test_none_is_refused_rather_than_raising(self) -> None:
        """A description is a bonus on top of a sighting. An exception here
        would cost the sighting that carries it."""
        assert colour_of(None) == (None, 0.0)

    def test_a_confidence_below_the_floor_reports_no_colour(self, monkeypatch) -> None:
        monkeypatch.setattr(attributes, "MIN_COLOUR_CONFIDENCE", 0.99)
        assert colour_of(block((255, 255, 255)))[0] == "white"
        monkeypatch.setattr(attributes, "MIN_COLOUR_CONFIDENCE", 1.01)
        assert colour_of(block((255, 255, 255)))[0] is None

    def test_a_shadowed_white_car_is_still_white(self) -> None:
        """Bodywork is never one value. White, silver and grey are the same
        paint under different light, so a spread across them must not be read
        as two colours disagreeing — that would refuse most real vehicles."""
        crop = np.zeros((60, 60, 3), dtype=np.uint8)
        crop[:, :20] = (250, 250, 250)   # glare on the bonnet
        crop[:, 20:40] = (210, 210, 210)  # lit panel
        crop[:, 40:] = (150, 150, 150)   # shadow under the arch
        assert colour_of(crop)[0] == "white"


class TestBodyBand:
    def test_the_road_under_a_vehicle_does_not_name_it(self) -> None:
        """The bottom of a vehicle box is tarmac and shadow. Histogramming the
        whole box made 40% of the simulated farm "grey"; the band is why it
        does not any more."""
        crop = np.zeros((80, 60, 3), dtype=np.uint8)
        crop[:20, :] = (200, 60, 40)      # roof and windscreen
        crop[20:60, :] = (200, 60, 40)    # bodywork — the band
        crop[60:, :] = (60, 60, 60)       # road
        assert colour_of(crop)[0] == "blue"


class TestDescribe:
    def test_the_detector_label_is_mapped_to_what_a_person_would_say(self) -> None:
        assert describe(block((10, 10, 10)), "motorcycle").vehicle_class == "two-wheeler"
        assert describe(block((10, 10, 10)), "car").vehicle_class == "car"

    def test_an_unknown_label_yields_no_class_rather_than_the_raw_label(self) -> None:
        """The mapping is the vocabulary. A label that is not in it is a label
        the watchlist cannot be written against, so passing it through would
        create a class that never matches anything."""
        assert describe(block((10, 10, 10)), "aeroplane").vehicle_class is None
        assert describe(block((10, 10, 10)), None).vehicle_class is None

    def test_described_is_false_only_when_nothing_is_known(self) -> None:
        assert not VehicleAttributes().described
        assert VehicleAttributes(colour="white").described
        assert VehicleAttributes(vehicle_class="bus").described

    def test_as_text_reads_the_way_an_operator_would_say_it(self) -> None:
        assert VehicleAttributes(colour="silver", vehicle_class="car").as_text() == "silver car"
        assert VehicleAttributes(vehicle_class="bus").as_text() == "bus"
        assert VehicleAttributes().as_text() == "unknown"
