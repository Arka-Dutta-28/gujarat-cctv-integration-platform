"""Appearance descriptors for vehicle re-identification.

The claim being tested is deliberately modest, and the tests are written to hold
it to exactly that: vehicles that look alike produce nearby vectors. Not that
the same vehicle is provably identified. This is a colour and shape signature,
not a learned re-ID embedding, and a test asserting more than the descriptor can
deliver would be the sort of check that passes while the feature misleads an
operator.

So what is pinned here is that colour separates, that brightness alone does not
dominate, that the vector is normalised so the database's cosine distance
behaves, and that a degenerate input returns nothing rather than a
plausible-looking vector.
"""

from __future__ import annotations

import pytest

from services.anpr.reid import EMBEDDING_DIM, cosine_distance, embed

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")


def block(colour: tuple[int, int, int], size: tuple[int, int] = (120, 200)) -> object:
    """A solid BGR rectangle.

    Only used for the degenerate cases: a crop with *no* variation is a blank
    frame by the descriptor's own definition, which is a property worth having
    and which makes this a poor stand-in for a real vehicle. Anything testing
    normal behaviour uses `noisy`.
    """
    image = np.zeros((size[0], size[1], 3), dtype=np.uint8)
    image[:, :] = colour
    return image


def noisy(colour: tuple[int, int, int], seed: int = 0) -> object:  # noqa: D401
    """The same colour with sensor noise and a brightness gradient.

    Closer to what two frames of one vehicle actually look like than two
    identical arrays would be.
    """
    rng = np.random.default_rng(seed)
    image = np.array(block(colour), dtype=np.int16)
    image += rng.integers(-18, 18, image.shape, dtype=np.int16)
    image += np.linspace(-25, 25, image.shape[0], dtype=np.int16)[:, None, None]
    return np.clip(image, 0, 255).astype(np.uint8)


class TestShape:
    def test_the_vector_has_the_dimension_the_column_declares(self) -> None:
        """A mismatch here is an insert that fails on every single sighting."""
        vector = embed(noisy((40, 40, 200)))
        assert vector is not None
        assert len(vector) == EMBEDDING_DIM == 64

    def test_it_is_unit_length(self) -> None:
        """Cosine distance is only meaningful against normalised vectors."""
        vector = embed(noisy((40, 160, 40)))
        assert vector is not None
        assert sum(x * x for x in vector) == pytest.approx(1.0, abs=1e-6)


class TestDiscrimination:
    def test_two_views_of_the_same_colour_vehicle_are_close(self) -> None:
        red_a, red_b = embed(noisy((30, 30, 200), 1)), embed(noisy((30, 30, 200), 2))
        assert cosine_distance(red_a, red_b) < 0.15

    def test_different_colours_are_far_apart(self) -> None:
        red, blue = embed(noisy((30, 30, 200), 1)), embed(noisy((200, 40, 30), 1))
        assert cosine_distance(red, blue) > 0.3

    def test_a_red_car_is_nearer_another_red_car_than_a_blue_one(self) -> None:
        """The property the shortlist actually depends on — a ranking, not a bar."""
        red = embed(noisy((30, 30, 200), 1))
        other_red = embed(noisy((35, 25, 205), 7))
        blue = embed(noisy((200, 40, 30), 7))
        assert cosine_distance(red, other_red) < cosine_distance(red, blue)

    def test_brightness_alone_moves_the_vector_less_than_hue_does(self) -> None:
        """The same car at noon and under sodium light must still match.

        This is why the histograms are HSV and weighted, rather than RGB: hue is
        comparatively stable across lighting, and brightness is not.
        """
        base = embed(noisy((30, 30, 200), 3))
        darker = embed(noisy((20, 20, 130), 3))
        different_hue = embed(noisy((30, 200, 30), 3))
        assert cosine_distance(base, darker) < cosine_distance(base, different_hue)


class TestDegenerateInput:
    def test_an_empty_crop_returns_nothing(self) -> None:
        assert embed(np.zeros((0, 0, 3), dtype=np.uint8)) is None

    def test_a_crop_too_small_to_describe_returns_nothing(self) -> None:
        """Better no descriptor than one computed from four pixels."""
        assert embed(np.zeros((4, 4, 3), dtype=np.uint8)) is None

    def test_none_returns_nothing(self) -> None:
        assert embed(None) is None

    def test_a_pure_black_crop_returns_nothing_rather_than_a_zero_vector(self) -> None:
        """A zero vector has no direction, so every cosine distance to it is 1.

        Stored, it would sit in the index matching nothing and looking like a
        descriptor. Absent, the sighting is honestly marked as having none.
        """
        assert embed(np.zeros((100, 100, 3), dtype=np.uint8)) is None


class TestCosineDistance:
    def test_identical_vectors_are_zero_apart(self) -> None:
        v = embed(noisy((60, 60, 180)))
        assert cosine_distance(v, v) == pytest.approx(0.0, abs=1e-9)

    def test_mismatched_or_missing_vectors_are_maximally_distant(self) -> None:
        """Never raises: this runs on the query path with data from the database."""
        assert cosine_distance([], [1.0]) == 1.0
        assert cosine_distance([1.0, 0.0], [1.0]) == 1.0
        assert cosine_distance([0.0, 0.0], [1.0, 0.0]) == 1.0


class TestDescriptorCoverage:
    """The descriptor must not depend on whether a plate was read.

    This is the bug the tests missed for the whole of M9, and the reason it was
    missed is worth stating: every camera in the test fixtures could read
    plates, so "computed when a plate was read" and "computed" were the same
    set. On the 30 government cameras they are not, and the difference is the
    entire population.
    """

    def test_a_track_that_never_read_a_plate_still_carries_a_descriptor(self) -> None:
        from services.anpr.attributes import VehicleAttributes
        from services.anpr.tracks import Track

        track = Track(track_id="t1", camera_id="cam", first_seen=0.0, last_seen=1.0)
        described = VehicleAttributes(colour="silver").with_embedding([0.1] * 4)
        track.observe(1.0, bbox=(0, 0, 100, 100), attributes=described)

        assert track.embedding == [0.1] * 4
        assert not track.reads, "no plate was read, and that is the point"

    def test_the_descriptor_and_the_colour_come_from_the_same_frame(self) -> None:
        """They are one measurement of one view. Kept apart they drifted onto
        two different frames, and one of them stopped being computed at all."""
        from services.anpr.attributes import VehicleAttributes
        from services.anpr.tracks import Track

        track = Track(track_id="t1", camera_id="cam", first_seen=0.0, last_seen=1.0)
        small = VehicleAttributes(colour="red").with_embedding([0.9] * 4)
        large = VehicleAttributes(colour="blue").with_embedding([0.2] * 4)
        track.observe(1.0, bbox=(0, 0, 20, 20), attributes=small)
        track.observe(2.0, bbox=(0, 0, 200, 200), attributes=large)

        assert (track.vehicle_colour, track.embedding) == ("blue", [0.2] * 4)

    def test_a_worse_view_does_not_replace_a_better_one(self) -> None:
        from services.anpr.attributes import VehicleAttributes
        from services.anpr.tracks import Track

        track = Track(track_id="t1", camera_id="cam", first_seen=0.0, last_seen=1.0)
        track.observe(1.0, bbox=(0, 0, 200, 200),
                      attributes=VehicleAttributes(colour="blue").with_embedding([0.2] * 4))
        track.observe(2.0, bbox=(0, 0, 20, 20),
                      attributes=VehicleAttributes(colour="red").with_embedding([0.9] * 4))

        assert (track.vehicle_colour, track.embedding) == ("blue", [0.2] * 4)
