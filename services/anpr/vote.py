"""Per-track plate voting.

The rule: aggregate every OCR read across a tracked vehicle's lifetime,
confidence weighted, into one plate string per track. Emitting per frame
produces duplicate sightings and worse accuracy.

Two votes run, and the stronger result wins.

Whole-string vote. Reads are normalised and grouped, and the group with the most
confidence behind it wins. This is the conservative answer, because it can only
return a string some frame actually produced.

Per-character vote. Among reads of the modal length, each slot is decided
independently. This can recover a plate no single frame got right: one frame
misreads position 3, another misreads position 7, and the consensus is correct.
On a moving vehicle, where the plate is legible in different parts of different
frames, this is where most of the accuracy comes from.

The per-character consensus is preferred only when it normalises to a valid
Indian plate and the whole-string winner does not, or when it carries more
weight. A consensus that invents a plate nobody saw and cannot be validated is
worse than an honest majority read.

Confidence is reported as something defensible rather than flattering: the share
of weight behind the winner, scaled by how confident those reads were, with a
bounded bonus for having seen the plate many times. It is never 1.0.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from services.common.plates import NormalisedPlate, normalise

__all__ = ["PlateRead", "TrackVote", "vote", "clean_for_vote"]

# A single read this weak is noise, not evidence. Kept low: the invariant is
# that reads are persisted, not discarded, and the vote is where quality is
# decided — but a 0.05-confidence read should not move a slot.
MIN_READ_CONFIDENCE = 0.10

# How much agreement across many frames is allowed to add. Seeing the same
# plate twenty times is genuinely stronger evidence than seeing it twice, but
# it is not proof, and a pipeline that reports 0.99 on a repeated misread is
# worse than one that reports 0.7 honestly.
MAX_REPETITION_BONUS = 0.12
REPETITION_SATURATES_AT = 8


@dataclass(frozen=True)
class PlateRead:
    """One OCR result from one frame of one tracked vehicle."""

    raw: str
    confidence: float
    frame_index: int = 0


@dataclass
class TrackVote:
    """The single sighting a whole track collapses into."""

    plate_raw: str
    plate: NormalisedPlate
    confidence: float
    read_count: int
    #: Share of the confidence weight that backed the winner, 0-1. Low
    #: agreement on a high-confidence plate means the reads disagreed, which is
    #: exactly when an operator should be shown the alternatives.
    agreement: float
    #: Whether the per-character consensus, not a single frame, produced this.
    from_consensus: bool = False
    alternatives: list[tuple[str, float]] = field(default_factory=list)

    @property
    def plate_normalised(self) -> str:
        return self.plate.normalised

    @property
    def format_valid(self) -> bool:
        return self.plate.format_valid


def clean_for_vote(raw: str) -> str:
    """Reduce a read to the characters a positional vote can work on."""
    return "".join(c for c in (raw or "").upper() if c.isalnum())


def _weighted_char_consensus(reads: list[PlateRead]) -> tuple[str, float] | None:
    """Vote each character slot independently among reads of the modal length.

    Returns the consensus string and the mean per-slot agreement, or None when
    there is not enough of a consensus to be worth considering.
    """
    lengths: dict[int, float] = defaultdict(float)
    for read in reads:
        lengths[len(clean_for_vote(read.raw))] += read.confidence
    lengths.pop(0, None)
    if not lengths:
        return None

    modal_length = max(lengths, key=lambda k: (lengths[k], k))
    same_length = [r for r in reads if len(clean_for_vote(r.raw)) == modal_length]
    # A "consensus" of one is just that read, and claiming otherwise would
    # inflate its confidence.
    if len(same_length) < 2:
        return None

    chars: list[str] = []
    agreements: list[float] = []
    for slot in range(modal_length):
        weights: dict[str, float] = defaultdict(float)
        for read in same_length:
            weights[clean_for_vote(read.raw)[slot]] += read.confidence
        total = sum(weights.values())
        winner = max(weights, key=lambda c: (weights[c], c))
        chars.append(winner)
        agreements.append(weights[winner] / total if total else 0.0)

    return "".join(chars), sum(agreements) / len(agreements)


def _confidence(share: float, mean_read_confidence: float, read_count: int) -> float:
    """Report a figure the pipeline can defend, not a flattering one."""
    seen = min(read_count, REPETITION_SATURATES_AT) / REPETITION_SATURATES_AT
    return round(min(share * mean_read_confidence + MAX_REPETITION_BONUS * seen, 0.99), 4)


def vote(reads: list[PlateRead]) -> TrackVote | None:
    """Collapse every read of one tracked vehicle into one sighting.

    Returns None only when the track produced nothing readable at all. A read
    that fails the Indian format check is *not* nothing: it is returned with
    `format_valid` false, at reduced confidence, because a mis-read wanted
    vehicle is worse than a noisy record.
    """
    usable = [r for r in reads if clean_for_vote(r.raw) and r.confidence >= MIN_READ_CONFIDENCE]
    if not usable:
        return None

    # --- whole-string vote ---
    by_key: dict[str, list[PlateRead]] = defaultdict(list)
    for read in usable:
        by_key[normalise(read.raw).normalised].append(read)

    total_weight = sum(r.confidence for r in usable)
    weights = {key: sum(r.confidence for r in rs) for key, rs in by_key.items()}
    best_key = max(weights, key=lambda k: (weights[k], k))
    best_reads = by_key[best_key]
    best_share = weights[best_key] / total_weight if total_weight else 0.0

    winner_raw = max(best_reads, key=lambda r: r.confidence).raw
    winner_plate = normalise(winner_raw)
    winner_share = best_share
    winner_reads = best_reads
    from_consensus = False

    # --- per-character vote ---
    consensus = _weighted_char_consensus(usable)
    if consensus is not None:
        text, slot_agreement = consensus
        candidate = normalise(text)
        prefer = (
            # A consensus that validates beats a majority read that does not:
            # this is the case it exists for.
            (candidate.format_valid and not winner_plate.format_valid)
            # Otherwise only when the slots agreed more strongly than the
            # whole-string groups did, and validity is not being given up.
            or (
                slot_agreement > winner_share
                and candidate.format_valid >= winner_plate.format_valid
            )
        )
        if prefer and candidate.normalised != winner_plate.normalised:
            winner_raw = text
            winner_plate = candidate
            winner_share = slot_agreement
            # Every read of the modal length contributed to this string.
            winner_reads = [r for r in usable if len(clean_for_vote(r.raw)) == len(text)]
            from_consensus = True

    mean_conf = sum(r.confidence for r in winner_reads) / len(winner_reads)
    alternatives = sorted(
        (
            (key, round(w / total_weight, 4))
            for key, w in weights.items()
            if key != winner_plate.normalised
        ),
        key=lambda kv: -kv[1],
    )[:3]

    return TrackVote(
        plate_raw=winner_raw,
        plate=winner_plate,
        confidence=_confidence(winner_share, mean_conf, len(usable)),
        read_count=len(usable),
        agreement=round(winner_share, 4),
        from_consensus=from_consensus,
        alternatives=alternatives,
    )
