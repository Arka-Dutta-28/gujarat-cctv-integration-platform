"""Match tiers: turning a plate read into an alert.

A watchlist match is not a boolean. A system that fires only on exact matches
misses the wanted vehicle whose plate came back one character wrong, which is
12.7% of reads on this estate (82.4% exact against 95.1% within edit distance
2). A system that fires on anything close floods the operator until they stop
reading alerts. Tiers exist so both reads reach a human with an honest label on
how far to trust them.

    confirmed   exact key and confidence >= 0.85    act on it
    probable    exact key at lower confidence,      verify against the crop
                or edit distance 1
    possible    edit distance 2                     a lead, corroborate first
    attribute   no plate; colour and class match    a filter, never alone

confirmed and probable differ on an *exact* key deliberately. A plate read at
confidence 0.4 that happens to land on a watchlist entry is not the same
evidence as one read at 0.95, even though the strings are identical: the low
confidence read is more likely to be a misread that coincidentally matched. The
tier says which of those the operator is looking at.

The attribute tier is not a fourth degree of the same thing. The three plate
tiers all answer "how close is this read to that plate" and differ only in
distance. attribute answers "this camera could not read a plate at all; does
the vehicle at least look like the one we want". It is categorically weaker,
because several thousand white hatchbacks pass a Gujarat highway camera in a
day and this tier calls every one of them a match.

It exists because 0 of the 30 government cameras reach ANPR grade. On those
feeds the choice is not between a strong signal and a weak one, but between a
weak signal and nothing. Three constraints make it safe, all enforced in
match_attributes rather than left to the caller:

1. It never originates an alert. An attribute match is raised only for a
   watchlist entry whose plate was matched recently (corroborated=True, which
   the writer establishes from its own record). Attributes extend a trace that a
   plate started; they cannot start one.
2. The watchlist entry must actually describe a vehicle. An entry carrying only
   a plate matches nothing here, which is the correct outcome: silence rather
   than a guess.
3. Every described field must agree. A "white truck" entry does not match a
   white car. Partial agreement on a description this coarse is not evidence.

priority_for demotes it by three, so an attribute match on the gravest entry
sorts below a confirmed match on a routine one.

Edit distance is still needed after normalisation. Positional normalisation
(invariant 3) repairs cross-class errors, a letter sitting in a digit slot. The
dominant residual OCR error on this estate is within-class: 0 read as 6, 8 read
as B inside the same slot type. Normalisation cannot touch those, because the
character is already the right class. That is the error edit distance catches,
and why probable exists rather than being folded into confirmed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from services.common.plates import edit_distance, plate_key_variants

__all__ = [
    "WatchlistEntry",
    "Match",
    "match_plate",
    "match_attributes",
    "CONFIRMED",
    "PROBABLE",
    "POSSIBLE",
    "ATTRIBUTE",
    "CONFIRMED_CONFIDENCE",
]

CONFIRMED = "confirmed"
PROBABLE = "probable"
POSSIBLE = "possible"
ATTRIBUTE = "attribute"

#: Confidence at or above which an exact key match is `confirmed` rather than
#: `probable`. From the honesty convention; the pipeline reports the *weakest character's*
#: probability as a plate's confidence, so this is a floor on every slot rather
#: than on an average that could hide one unreadable character.
CONFIRMED_CONFIDENCE = float(os.environ.get("ALERT_CONFIRMED_CONFIDENCE", "0.85"))

#: Severity runs 1 (routine) to 5 (grave), as the schema's CHECK constrains.
#: Priority is what an alert console sorts by, and it must fold in *both* how
#: serious the vehicle is and how sure we are it is that vehicle: a `possible`
#: match on a stolen-car entry should not outrank a `confirmed` match on the
#: same entry. Tier therefore demotes.
TIER_DEMOTION = {CONFIRMED: 0, PROBABLE: 1, POSSIBLE: 2, ATTRIBUTE: 3}
MIN_PRIORITY, MAX_PRIORITY = 1, 5


@dataclass(frozen=True)
class WatchlistEntry:
    """One active watchlist row, as the matcher holds it in memory."""

    id: str
    plate: str
    plate_normalised: str
    category: str
    severity: int = 3
    case_ref: str | None = None
    #: What the vehicle looks like, when the source recorded it. Almost always
    #: absent — VAHAN carries a colour, a manually-entered "suspect" row
    #: usually does not — and absent means this entry can never be matched on
    #: appearance, which is the safe default rather than a gap to fill in.
    vehicle_colour: str | None = None
    vehicle_class: str | None = None

    @property
    def describes_a_vehicle(self) -> bool:
        """Whether this entry says enough about appearance to match on it."""
        return bool(self.vehicle_colour and self.vehicle_class)


@dataclass(frozen=True)
class Match:
    entry: WatchlistEntry
    tier: str
    priority: int
    distance: int
    reason: str


def priority_for(severity: int, tier: str) -> int:
    """Console sort order: severity, demoted by how uncertain the match is."""
    demoted = severity - TIER_DEMOTION[tier]
    return max(MIN_PRIORITY, min(MAX_PRIORITY, demoted))


def match_plate(
    plate_normalised: str,
    confidence: float,
    entries: list[WatchlistEntry],
    *,
    identifying: bool = True,
) -> Match | None:
    """The best watchlist match for one read, or None.

    `identifying=False` never matches. Those are reads shorter than four
    characters, which the wide-area government cameras produce in volume: a read
    of `GJ` is within edit distance 2 of a great many watchlist entries and
    within edit distance 0 of none of them. Alerting on it would raise a
    confident-looking alert from a camera that cannot resolve a plate at all.
    The read is still persisted and still searchable (invariant 1) — it is only
    barred from *driving* an alert.
    """
    if not identifying:
        return None
    key = (plate_normalised or "").strip().upper()
    if not key:
        return None

    # The operator's plate and the stored key are both normalised, but a
    # watchlist entry may have been typed in a form that normalises differently;
    # `plate_key_variants` is the same helper the search path uses, so alerting
    # and trace agree about what "the same plate" means.
    variants = set(plate_key_variants(key))

    best: Match | None = None
    for entry in entries:
        target = entry.plate_normalised.strip().upper()
        if not target:
            continue

        if target in variants or target == key:
            distance = 0
            tier = CONFIRMED if confidence >= CONFIRMED_CONFIDENCE else PROBABLE
            reason = (
                f"exact match on {target} at confidence {confidence:.2f}"
                if tier == CONFIRMED
                else (
                    f"exact match on {target}, but confidence {confidence:.2f} is "
                    f"below the {CONFIRMED_CONFIDENCE:.2f} bar for a confirmed match"
                )
            )
        else:
            distance = edit_distance(key, target, cap=3)
            if distance == 1:
                tier, reason = PROBABLE, f"one character from {target} (read {key})"
            elif distance == 2:
                tier, reason = POSSIBLE, f"two characters from {target} (read {key})"
            else:
                continue

        candidate = Match(
            entry=entry, tier=tier, distance=distance,
            priority=priority_for(entry.severity, tier), reason=reason,
        )
        # Closest match wins; between equals, the more serious entry. Ordering by
        # distance before severity matters: a grave entry two characters away
        # must not outrank the exact match sitting next to it in the list.
        if best is None or (candidate.distance, -candidate.priority) < (
            best.distance, -best.priority
        ):
            best = candidate
    return best


def match_attributes(
    colour: str | None,
    vehicle_class: str | None,
    entries: list[WatchlistEntry],
    *,
    corroborated: set[str] | None = None,
) -> Match | None:
    """The best appearance match for a sighting that carries no plate, or None.

    `corroborated` is the set of watchlist entry ids whose *plate* has been
    matched recently enough for an appearance match on the same entry to mean
    something. Nothing outside that set can match here, and passing None — the
    default — matches nothing at all. That is the whole safety property of this
    tier expressed as a signature: a caller cannot accidentally get a cold
    attribute alert, it has to hand over evidence that a plate match already
    happened.

    The three constraints from the module docstring are checked in the order
    that rejects most cheaply, because this runs on the write path for every
    unread vehicle on a busy road.
    """
    if not corroborated or not entries:
        return None
    have_colour = (colour or "").strip().lower()
    have_class = (vehicle_class or "").strip().lower()
    if not have_colour or not have_class:
        # A description with a hole in it cannot satisfy "every described field
        # agrees", so there is nothing to do. Deliberately not falling back to
        # matching on whichever field is present: "a truck" is not a lead.
        return None

    best: Match | None = None
    for entry in entries:
        if entry.id not in corroborated or not entry.describes_a_vehicle:
            continue
        if (entry.vehicle_colour or "").strip().lower() != have_colour:
            continue
        if (entry.vehicle_class or "").strip().lower() != have_class:
            continue

        candidate = Match(
            entry=entry,
            tier=ATTRIBUTE,
            # Not a plate distance at all. Reported as the worst distance any
            # tier uses so that the writer's "closest match wins" ordering puts
            # every plate match ahead of every appearance match without needing
            # to know this tier exists.
            distance=3,
            priority=priority_for(entry.severity, ATTRIBUTE),
            reason=(
                f"no plate read; appearance matches {entry.plate} "
                f"({have_colour} {have_class}), and that plate was matched "
                f"recently — corroboration only, not an identification"
            ),
        )
        if best is None or candidate.priority > best.priority:
            best = candidate
    return best
