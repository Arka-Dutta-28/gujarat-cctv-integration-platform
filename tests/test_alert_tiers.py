"""Match tiers — a watchlist hit is not a boolean.

The properties here are the ones that decide whether an operator trusts the
alert console. Two failure modes bracket the design: a matcher that only fires
on exact strings misses the wanted vehicle whose plate came back one character
wrong (12.7% of reads on this estate), and one that fires on anything close
buries the real hit in noise until nobody reads alerts at all.
"""

from __future__ import annotations

from services.alerting.tiers import (
    ATTRIBUTE,
    CONFIRMED,
    CONFIRMED_CONFIDENCE,
    POSSIBLE,
    PROBABLE,
    WatchlistEntry,
    match_attributes,
    match_plate,
    priority_for,
)

STOLEN = WatchlistEntry(
    id="wl-1", plate="GJ01AB1234", plate_normalised="GJ01AB1234",
    category="stolen", severity=5, case_ref="FIR-77/2026",
)


class TestTiers:
    def test_an_exact_read_at_high_confidence_is_confirmed(self) -> None:
        hit = match_plate("GJ01AB1234", 0.94, [STOLEN])
        assert hit is not None
        assert hit.tier == CONFIRMED
        assert hit.distance == 0

    def test_the_same_string_read_badly_is_only_probable(self) -> None:
        """The part that looks redundant and is not.

        A plate read at confidence 0.4 that lands on a watchlist entry is weaker
        evidence than the identical string read at 0.95 — it is likelier to be a
        misread that coincidentally matched. The tier says which one an operator
        is looking at, rather than presenting both as the same fact.
        """
        hit = match_plate("GJ01AB1234", 0.40, [STOLEN])
        assert hit is not None
        assert hit.tier == PROBABLE
        assert "below" in hit.reason

    def test_the_confidence_boundary_is_inclusive(self) -> None:
        assert match_plate("GJ01AB1234", CONFIRMED_CONFIDENCE, [STOLEN]).tier == CONFIRMED

    def test_one_character_out_is_probable(self) -> None:
        """`0` read as `6` inside a digit slot — the dominant residual error.

        Positional normalisation cannot repair this: the character is already
        the right class, so there is nothing structural to coerce. Edit distance
        is what surfaces it, which is the reason this tier exists.
        """
        hit = match_plate("GJ01AB1284", 0.9, [STOLEN])
        assert hit is not None
        assert hit.tier == PROBABLE
        assert hit.distance == 1

    def test_two_characters_out_is_possible(self) -> None:
        hit = match_plate("GJ01AB8284", 0.9, [STOLEN])
        assert hit is not None
        assert hit.tier == POSSIBLE

    def test_three_characters_out_does_not_alert(self) -> None:
        assert match_plate("GJ01AB8285", 0.9, [STOLEN]) is None

    def test_an_unrelated_plate_does_not_alert(self) -> None:
        assert match_plate("MH12XY9999", 0.99, [STOLEN]) is None


class TestSelection:
    def test_the_closest_entry_wins_over_the_more_serious_one(self) -> None:
        """A grave entry two characters away must not outrank the exact match."""
        routine = WatchlistEntry(
            id="wl-2", plate="GJ01AB1284", plate_normalised="GJ01AB1284",
            category="suspect", severity=1,
        )
        hit = match_plate("GJ01AB1284", 0.99, [STOLEN, routine])
        assert hit is not None
        assert hit.entry.id == "wl-2"
        assert hit.distance == 0

    def test_between_equally_close_entries_the_graver_one_wins(self) -> None:
        mild = WatchlistEntry(
            id="wl-3", plate="GJ01AB1235", plate_normalised="GJ01AB1235",
            category="other", severity=1,
        )
        hit = match_plate("GJ01AB1334", 0.9, [mild, STOLEN])
        assert hit is not None
        assert hit.entry.id == "wl-1"


class TestPriority:
    def test_certainty_demotes_priority_within_one_entry(self) -> None:
        """A `possible` hit on a stolen car must not sort above a `confirmed`."""
        assert priority_for(5, CONFIRMED) == 5
        assert priority_for(5, PROBABLE) == 4
        assert priority_for(5, POSSIBLE) == 3

    def test_priority_never_leaves_the_schema_range(self) -> None:
        assert priority_for(1, POSSIBLE) == 1
        assert priority_for(5, CONFIRMED) == 5


class TestNonIdentifyingReads:
    def test_a_read_too_short_to_identify_never_alerts(self) -> None:
        """The government cameras produce these in volume.

        A two-character read sits within edit distance 2 of a great many
        watchlist entries and within distance 0 of none. Alerting on it would
        raise a confident-looking alert from a camera measured as unable to
        resolve a plate at all — see services/anpr/capability.py.
        """
        assert match_plate("GJ", 0.9, [STOLEN], identifying=False) is None

    def test_the_read_is_still_a_read_the_flag_only_bars_alerting(self) -> None:
        """Invariant 1 is about persistence, and this module does not persist."""
        short = WatchlistEntry(
            id="wl-4", plate="GJ", plate_normalised="GJ", category="other", severity=2,
        )
        assert match_plate("GJ", 0.9, [short], identifying=True) is not None

    def test_an_empty_plate_never_alerts(self) -> None:
        assert match_plate("", 0.9, [STOLEN]) is None
        assert match_plate("   ", 0.9, [STOLEN]) is None


class TestNormalisationAgreement:
    def test_a_watchlist_entry_typed_with_spaces_still_matches(self) -> None:
        """Alerting and search must agree about what "the same plate" means."""
        spaced = WatchlistEntry(
            id="wl-5", plate="GJ 01 AB 1234", plate_normalised="GJ01AB1234",
            category="wanted", severity=4,
        )
        hit = match_plate("GJ01AB1234", 0.95, [spaced])
        assert hit is not None
        assert hit.tier == CONFIRMED


class TestTheAttributeTier:
    """The fourth tier, and the three constraints that make it safe.

    This tier can match several thousand vehicles a day on a single Gujarat
    highway camera. Everything below pins a way it is stopped from doing that,
    so a later change that loosens one has to delete a test that says why.
    """

    DESCRIBED = WatchlistEntry(
        id="wl-desc", plate="GJ05UV9972", plate_normalised="GJ05UV9972",
        category="stolen", severity=5, vehicle_colour="white", vehicle_class="truck",
    )
    PLATE_ONLY = WatchlistEntry(
        id="wl-plain", plate="GJ01AA1111", plate_normalised="GJ01AA1111",
        category="stolen", severity=5,
    )

    def test_a_corroborated_description_matches(self) -> None:
        hit = match_attributes("white", "truck", [self.DESCRIBED], corroborated={"wl-desc"})
        assert hit is not None
        assert hit.tier == ATTRIBUTE
        assert hit.entry.id == "wl-desc"

    def test_it_can_never_originate_an_alert(self) -> None:
        """Constraint 1. Without a recent plate match on the same entry there
        is no match at all — a cold appearance hit on the other side of the
        state is exactly the false lead this tier would otherwise produce."""
        assert match_attributes("white", "truck", [self.DESCRIBED]) is None
        assert match_attributes(
            "white", "truck", [self.DESCRIBED], corroborated=set()
        ) is None
        assert match_attributes(
            "white", "truck", [self.DESCRIBED], corroborated={"someone-else"}
        ) is None

    def test_an_entry_that_describes_no_vehicle_matches_nothing(self) -> None:
        """Constraint 2. Most watchlist entries carry only a plate, and silence
        is the correct outcome for them rather than a guess."""
        assert match_attributes(
            "white", "truck", [self.PLATE_ONLY], corroborated={"wl-plain"}
        ) is None

    def test_a_half_described_entry_matches_nothing(self) -> None:
        half = WatchlistEntry(
            id="wl-half", plate="GJ02BB2222", plate_normalised="GJ02BB2222",
            category="stolen", severity=5, vehicle_colour="white",
        )
        assert match_attributes(
            "white", "truck", [half], corroborated={"wl-half"}
        ) is None

    def test_every_described_field_must_agree(self) -> None:
        """Constraint 3. Not a best-of: partial agreement on a description this
        coarse is not evidence."""
        assert match_attributes(
            "white", "car", [self.DESCRIBED], corroborated={"wl-desc"}
        ) is None
        assert match_attributes(
            "silver", "truck", [self.DESCRIBED], corroborated={"wl-desc"}
        ) is None

    def test_a_sighting_with_half_a_description_matches_nothing(self) -> None:
        """`a truck` is not a lead, so there is no fallback to whichever field
        the sighting happens to carry."""
        assert match_attributes(
            None, "truck", [self.DESCRIBED], corroborated={"wl-desc"}
        ) is None
        assert match_attributes(
            "white", None, [self.DESCRIBED], corroborated={"wl-desc"}
        ) is None

    def test_it_sorts_below_every_plate_tier(self) -> None:
        """An attribute match on the gravest entry must sort below a confirmed
        match on a routine one. That is the intended reading: a filter for a
        human, not a call to act."""
        grave = match_attributes(
            "white", "truck", [self.DESCRIBED], corroborated={"wl-desc"}
        )
        routine = WatchlistEntry(
            id="wl-r", plate="GJ03CC3333", plate_normalised="GJ03CC3333",
            category="other", severity=3,
        )
        confirmed = match_plate("GJ03CC3333", 0.99, [routine])
        assert grave is not None and confirmed is not None
        assert grave.priority < confirmed.priority

    def test_its_distance_keeps_plate_matches_ahead_of_it(self) -> None:
        """The writer picks the closest match without knowing this tier exists,
        so an appearance match reports the worst distance any tier uses."""
        hit = match_attributes("white", "truck", [self.DESCRIBED], corroborated={"wl-desc"})
        assert hit is not None
        assert hit.distance == 3

    def test_the_reason_says_it_is_not_an_identification(self) -> None:
        hit = match_attributes("white", "truck", [self.DESCRIBED], corroborated={"wl-desc"})
        assert hit is not None
        assert "not an identification" in hit.reason
        assert "white truck" in hit.reason
