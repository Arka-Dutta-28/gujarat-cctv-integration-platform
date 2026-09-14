"""Per-track plate voting.

The rule under test is per-track voting: one sighting per tracked vehicle, decided
by a confidence-weighted vote across every read of that track. Getting this
wrong is expensive in both directions — per-frame emission floods `sightings`
with duplicates, and a naive vote throws away the frames where the plate was
actually legible.
"""

from __future__ import annotations

from services.anpr.vote import PlateRead, vote


def reads(*pairs: tuple[str, float]) -> list[PlateRead]:
    return [PlateRead(raw=r, confidence=c, frame_index=i) for i, (r, c) in enumerate(pairs)]


class TestBasics:
    def test_a_track_with_nothing_readable_produces_no_sighting(self) -> None:
        assert vote([]) is None
        assert vote(reads(("", 0.9), ("   ", 0.8))) is None

    def test_reads_below_the_noise_floor_are_ignored(self) -> None:
        assert vote(reads(("GJ01AB1234", 0.02))) is None

    def test_one_track_collapses_to_one_sighting(self) -> None:
        result = vote(reads(*[("GJ01AB1234", 0.8)] * 12))
        assert result is not None
        assert result.plate_normalised == "GJ01AB1234"
        assert result.read_count == 12

    def test_the_majority_read_wins(self) -> None:
        result = vote(reads(
            ("GJ01AB1234", 0.9), ("GJ01AB1234", 0.85), ("GJ01AB1284", 0.4),
        ))
        assert result is not None
        assert result.plate_normalised == "GJ01AB1234"

    def test_confidence_beats_count(self) -> None:
        """Two glimpses at 0.3 must not outvote one clear look at 0.95."""
        result = vote(reads(
            ("GJ05CD5678", 0.95), ("GJ05CD5G78", 0.3), ("GJ05CD5G78", 0.3),
        ))
        assert result is not None
        assert result.plate_normalised == "GJ05CD5678"


class TestCharacterConsensus:
    def test_a_plate_no_single_frame_read_correctly_is_recovered(self) -> None:
        """The case per-character voting exists for.

        Each frame gets a different character wrong, and no read is right; the
        slot-by-slot majority is. On a moving vehicle this is the normal case,
        not an edge case.
        """
        result = vote(reads(
            ("GJ01AB1734", 0.7),   # position 7 wrong
            ("GJ01AB1234", 0.7),   # correct
            ("GJ01A81234", 0.7),   # position 5 wrong
            ("GJ01AB1234", 0.7),   # correct
        ))
        assert result is not None
        assert result.plate_normalised == "GJ01AB1234"

    def test_consensus_is_preferred_when_it_validates_and_the_majority_does_not(self) -> None:
        result = vote(reads(
            ("GJ01AB12E4", 0.6),
            ("GJ01AB12E4", 0.6),
            ("GJ01AB1234", 0.55),
            ("GJ01AB1284", 0.55),
        ))
        assert result is not None
        assert result.format_valid

    def test_a_single_read_is_never_called_a_consensus(self) -> None:
        result = vote(reads(("GJ01AB1234", 0.9)))
        assert result is not None
        assert result.from_consensus is False

    def test_reads_of_different_lengths_do_not_vote_against_each_other(self) -> None:
        """A truncated read must not drag a slot; it is missing, not wrong."""
        result = vote(reads(
            ("GJ01AB1234", 0.8), ("GJ01AB1234", 0.8), ("AB1234", 0.7), ("01AB1234", 0.6),
        ))
        assert result is not None
        assert result.plate_normalised == "GJ01AB1234"


class TestHonestConfidence:
    def test_confidence_is_never_certain(self) -> None:
        result = vote(reads(*[("GJ01AB1234", 1.0)] * 50))
        assert result is not None
        assert result.confidence <= 0.99

    def test_disagreement_lowers_confidence(self) -> None:
        agreed = vote(reads(("GJ01AB1234", 0.8), ("GJ01AB1234", 0.8), ("GJ01AB1234", 0.8)))
        disputed = vote(reads(("GJ01AB1234", 0.8), ("GJ07XY9999", 0.79), ("MH12CD3456", 0.78)))
        assert agreed is not None and disputed is not None
        assert disputed.confidence < agreed.confidence

    def test_agreement_is_reported_so_an_operator_can_see_the_dispute(self) -> None:
        result = vote(reads(("GJ01AB1234", 0.8), ("GJ07XY9999", 0.79)))
        assert result is not None
        assert result.agreement < 0.75
        assert result.alternatives

    def test_seeing_a_plate_more_often_raises_confidence_but_only_so_far(self) -> None:
        few = vote(reads(*[("GJ01AB1234", 0.6)] * 2))
        many = vote(reads(*[("GJ01AB1234", 0.6)] * 20))
        assert few is not None and many is not None
        assert many.confidence > few.confidence
        assert many.confidence - few.confidence < 0.2


class TestFormatFailuresAreKept:
    """Invariant: a plate that fails the format check is flagged, never dropped."""

    def test_an_unparseable_read_still_produces_a_sighting(self) -> None:
        result = vote(reads(("XX!!99", 0.8), ("XX!!99", 0.75)))
        assert result is not None
        assert result.format_valid is False
        assert result.plate_normalised

    def test_an_overlay_timestamp_is_kept_and_flagged_rather_than_silently_dropped(self) -> None:
        """Burnt-in overlays get OCR'd; the record must say the read is junk."""
        result = vote(reads(("14-06-2026", 0.6), ("14-06-2026", 0.6)))
        assert result is not None
        assert result.format_valid is False
