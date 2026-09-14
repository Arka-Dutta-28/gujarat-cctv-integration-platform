"""Which characters OCR confuses with which, as data rather than a typed-in list.

Positional normalisation (invariant 3) has to answer one question over and over:
given that this slot must hold a digit, and OCR returned A, what digit was it,
and how much should we doubt the answer? The platform used to answer it from a
table written by hand, A to 4, S to 5, O to 0, with a flat cost for every pair.

That table was not wrong, but it was unjustified, and unjustified is a problem
for two reasons. Nobody can tell whether a missing pair is a considered omission
or an oversight. And every pair carried the same weight, so O to 0
(near-certain) and J to 1 (a stretch) were treated as equally likely readings,
which is exactly the judgement the split search in plates.py is trying to make.

So the table is now derived and shipped as data, from either of two sources, and
which one produced it is recorded in the file.

glyph. The characters are rendered in a plate-like face and compared as images.
Two characters that OCR confuses are, overwhelmingly, two characters that look
alike, and that is a measurable property of the glyphs rather than an opinion.
The cost of reading X where Y was printed falls with their visual similarity, so
O/0 is cheap and J/1 is not, without anyone deciding so.

empirical. The accuracy harness aligns what OCR returned against known ground
truth and counts the substitutions that actually happened. Strictly better than
glyph similarity where there is enough data, because it captures this engine's
real error distribution on this font at these resolutions.

Regenerate either with scripts/learn_confusion.py. If the data file is absent or
unreadable the built-in prior below is used, so behaviour never depends on a
generated file being present; it only gets better when one is.

The prior itself is kept, and kept small, because it is the floor: a confusion
model that has never seen data still has to normalise plates on day one.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("plates.confusion")

__all__ = [
    "ConfusionModel",
    "model",
    "merge",
    "from_similarity",
    "CONFUSION_PATH",
    "DIGITS",
    "LETTERS",
]

CONFUSION_PATH = Path(os.environ.get("OCR_CONFUSION_PATH", "data/ocr-confusion.json"))

DIGITS = "0123456789"
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# --- costs --------------------------------------------------------------
# In units the split search in `plates.py` compares directly. A character
# already of the right class is free; one that is a plausible confusion is
# cheap; one that is neither is expensive, because that split is probably wrong.

COST_OK = 0.0
#: Cost of the *most* plausible confusion. Anything derived scales up from here.
COST_BEST_CONFUSION = 1.0
#: Cost of the least plausible confusion still worth allowing.
COST_WORST_CONFUSION = 3.0
#: Cost of a coercion the model has no evidence for at all.
COST_IMPLAUSIBLE = 6.0

#: Similarity below which two glyphs are not considered confusable at all.
#: Everything above it is mapped onto the cost band above.
MIN_SIMILARITY = 0.55

#: How many neighbours a letter may have in the letter-to-letter confusion set.
#:
#: Letter pairs need a far stricter rule than cross-class coercions, and the
#: reason is asymmetric risk. Coercing a letter into a digit inside a digit slot
#: is recoverable: the slot had to hold a digit anyway, and getting it wrong
#: costs one character of a fuzzy match. But a letter pair is what licenses
#: repairing a *state code*, and there the wrong answer turns one real state
#: into a different real state — `GJ` into `GA` — which is a confidently wrong
#: plate rather than a doubtful one.
#:
#: So the rule is **mutual top-k**: `A` and `B` are confusable only if each is
#: among the other's k most similar letters. Relative rather than absolute,
#: because at the sizes plate characters actually occupy every letter is
#: somewhat similar to every other one and an absolute threshold either admits
#: all of them or none.
LETTER_NEIGHBOURS = int(os.environ.get("OCR_LETTER_NEIGHBOURS", "3"))

#: Quantile a cross-class pair must reach before it becomes a *coercion target*
#: — that is, before "this letter, in a digit slot, is that digit".
#:
#: Costs are recorded for every pair above `MIN_SIMILARITY`; coercion targets
#: are held to a much higher bar, and the reason is that a coercion map with no
#: gaps destroys the platform's ability to reject nonsense. If every letter has
#: some digit it can become, then every string of the right length coerces into
#: a structurally valid plate — `!!GARBAGE!!` normalises to `GA 8 4863`, gets
#: `format_valid` true, and the flag that tells downstream code to distrust a
#: read stops meaning anything. Leaving the weak pairs out is what keeps
#: `COST_IMPLAUSIBLE` a real signal.
MAP_QUANTILE = float(os.environ.get("OCR_MAP_QUANTILE", "0.75"))

# --- the prior ----------------------------------------------------------
# The floor, used when no generated table is present. Deliberately moderate:
# every extra pair is another chance of a false collision between two real
# plates. Costs are uniform here precisely because a hand-written table has no
# principled way to rank them — that is what the generated tables add.

_PRIOR_TO_DIGIT = {
    "A": "4", "B": "8", "C": "0", "D": "0", "E": "3", "G": "6", "I": "1",
    "J": "1", "L": "1", "O": "0", "Q": "0", "S": "5", "T": "7", "U": "0",
    "Z": "2",
}

_PRIOR_TO_LETTER = {
    "0": "O", "1": "I", "2": "Z", "3": "B", "4": "A", "5": "S", "6": "G",
    "7": "T", "8": "B", "9": "G",
}

# Letters OCR genuinely confuses with each other. Letter-to-letter, so
# positional coercion cannot help: both characters are already the right class
# for a letter slot. Used to repair state codes against the closed list of real
# ones.
_PRIOR_LETTER_PAIRS = [
    "IJ", "IL", "IT", "OD", "OQ", "CG", "BR", "EF",
    "MN", "UV", "SZ", "KX", "PR", "DP", "HN",
]

#: Cost assigned to every prior pair, since the prior cannot rank them.
_PRIOR_COST = 2.0


@dataclass
class ConfusionModel:
    """What OCR confuses with what, and how much each confusion costs.

    `costs` is keyed `"<observed><target>"`. A pair absent from it is not
    impossible, merely unsupported — it costs `COST_IMPLAUSIBLE`, which is high
    enough to lose to any supported alternative and low enough that a plate with
    one unexplained character is still read rather than discarded.
    """

    #: Best digit for each letter, and best letter for each digit.
    to_digit: dict[str, str] = field(default_factory=dict)
    to_letter: dict[str, str] = field(default_factory=dict)
    #: Unordered letter-to-letter confusions, as sorted two-character strings.
    letter_pairs: set[str] = field(default_factory=set)
    costs: dict[str, float] = field(default_factory=dict)
    #: `prior`, `glyph` or `empirical`. Reported so a measurement can say which
    #: table produced it.
    source: str = "prior"
    #: Free-text note from the generator: font used, sample size, date.
    provenance: str = ""
    #: True when this table was derived from enough real evidence to replace
    #: the built-in prior rather than being merged over it. See `merge`.
    standalone: bool = False

    # --- the interface `plates.py` uses ---

    def coerce_to_digit(self, ch: str) -> str:
        return ch if ch.isdigit() else self.to_digit.get(ch, ch)

    def coerce_to_letter(self, ch: str) -> str:
        return ch if ch.isalpha() else self.to_letter.get(ch, ch)

    def char_cost(self, ch: str, wants_digit: bool) -> float:
        """What it costs to read `ch` as the class this slot needs."""
        if ch.isdigit() == wants_digit:
            return COST_OK
        target = self.to_digit.get(ch) if wants_digit else self.to_letter.get(ch)
        if target is None:
            return COST_IMPLAUSIBLE
        return self.costs.get(ch + target, _PRIOR_COST)

    def confusable(self, a: str, b: str) -> bool:
        """True if these two letters are ones OCR mixes up."""
        return a == b or "".join(sorted((a, b))) in self.letter_pairs

    def describe(self) -> dict[str, object]:
        return {
            "source": self.source,
            "provenance": self.provenance,
            "digit_coercions": len(self.to_digit),
            "letter_coercions": len(self.to_letter),
            "letter_pairs": len(self.letter_pairs),
        }


def _prior() -> ConfusionModel:
    costs = {
        **{observed + target: _PRIOR_COST for observed, target in _PRIOR_TO_DIGIT.items()},
        **{observed + target: _PRIOR_COST for observed, target in _PRIOR_TO_LETTER.items()},
    }
    return ConfusionModel(
        to_digit=dict(_PRIOR_TO_DIGIT),
        to_letter=dict(_PRIOR_TO_LETTER),
        letter_pairs={"".join(sorted(pair)) for pair in _PRIOR_LETTER_PAIRS},
        costs=costs,
        source="prior",
        provenance="built-in prior; run scripts/learn_confusion.py to derive one",
    )


def from_similarity(
    similarity: dict[str, float],
    *,
    source: str,
    provenance: str = "",
    standalone: bool = False,
) -> ConfusionModel:
    """Build a model from pairwise character similarity in [0, 1].

    `similarity` is keyed `"<a><b>"` for the ordered pair "a was printed, b was
    read" — or symmetrically, for a glyph comparison. Pairs below
    `MIN_SIMILARITY` are dropped: a model that allows every coercion is a model
    that collapses distinct plates onto one key, which is the failure invariant
    3 exists to prevent.

    Cost falls linearly with similarity across the band between
    `COST_BEST_CONFUSION` and `COST_WORST_CONFUSION`, so the search in
    `plates.py` prefers the reading that looks more like what was printed.
    """
    kept = {pair: score for pair, score in similarity.items() if score >= MIN_SIMILARITY}
    if not kept:
        return _prior()

    best_digit: dict[str, tuple[float, str]] = {}
    best_letter: dict[str, tuple[float, str]] = {}
    costs: dict[str, float] = {}

    top = max(kept.values())
    span = max(1e-6, top - MIN_SIMILARITY)

    # Computed over cross-class pairs only. A quantile taken across everything
    # would be dominated by the 650 letter-to-letter pairs, and would gate
    # letter-to-digit coercions on a distribution they are not part of.
    cross = sorted(
        score
        for pair, score in kept.items()
        if len(pair) == 2 and (pair[0] in LETTERS) != (pair[1] in LETTERS)
    )
    map_floor = (
        cross[min(len(cross) - 1, int(len(cross) * MAP_QUANTILE))] if cross else 0.0
    )

    for pair, score in kept.items():
        if len(pair) != 2:
            continue
        observed, target = pair[0], pair[1]
        # Linear in similarity, best-scoring pair at the cheap end of the band.
        cost = COST_WORST_CONFUSION - (score - MIN_SIMILARITY) / span * (
            COST_WORST_CONFUSION - COST_BEST_CONFUSION
        )
        costs[pair] = round(cost, 3)

        if score < map_floor:
            # Recorded as a cost, but not strong enough to be the answer to
            # "what was this character really".
            continue

        if (
            observed in LETTERS
            and target in DIGITS
            and score > best_digit.get(observed, (0.0, ""))[0]
        ):
            best_digit[observed] = (score, target)

    # The digit -> letter direction is resolved *after* the letter -> digit one
    # and constrained by it, rather than being a second independent argmax.
    #
    # Taken independently the two directions disagree: `S`'s closest digit is
    # `5`, but `8`'s closest letter also comes out `S`, so `B` — which is
    # unambiguously what an `8` is misread as — never gets claimed by anything.
    # OCR confusion is overwhelmingly *pairwise*: characters come in look-alike
    # couples, and a couple that has already matched should not be the answer
    # to a third character's question as well. So a digit prefers a letter that
    # picked it back, and only falls through to an unconstrained best if no
    # letter did.
    claimed: dict[str, list[tuple[float, str]]] = {}
    for letter, (score, digit) in best_digit.items():
        claimed.setdefault(digit, []).append((score, letter))

    for pair, score in kept.items():
        if len(pair) != 2 or score < map_floor:
            continue
        digit, letter = pair[0], pair[1]
        if digit in DIGITS and letter in LETTERS:
            mutual = claimed.get(digit)
            if mutual and letter not in {name for _, name in mutual}:
                continue
            if score > best_letter.get(digit, (0.0, ""))[0]:
                best_letter[digit] = (score, letter)

    letter_pairs = _mutual_letter_pairs(kept)

    return ConfusionModel(
        to_digit={k: v[1] for k, v in best_digit.items()},
        to_letter={k: v[1] for k, v in best_letter.items()},
        letter_pairs=letter_pairs,
        costs=costs,
        source=source,
        provenance=provenance,
        standalone=standalone,
    )


def _mutual_letter_pairs(similarity: dict[str, float]) -> set[str]:
    """Letter-to-letter confusions, by mutual nearest neighbours.

    Ranked among *letters only*: a letter's most similar glyph overall is often
    a digit, and that tells us nothing about which letters can be mistaken for
    each other inside a slot that must hold a letter.
    """
    neighbours: dict[str, list[tuple[float, str]]] = {}
    for pair, score in similarity.items():
        if len(pair) != 2:
            continue
        a, b = pair[0], pair[1]
        if a in LETTERS and b in LETTERS and a != b:
            neighbours.setdefault(a, []).append((score, b))

    top: dict[str, set[str]] = {
        letter: {b for _, b in sorted(scored, reverse=True)[:LETTER_NEIGHBOURS]}
        for letter, scored in neighbours.items()
    }
    return {
        "".join(sorted((a, b)))
        for a, closest in top.items()
        for b in closest
        if a in top.get(b, set())
    }


def merge(base: ConfusionModel, overlay: ConfusionModel) -> ConfusionModel:
    """Overlay a derived table onto the prior, keeping the prior as a floor.

    A generated table is better evidence than a hand-written one about how much to
    trust each confusion, and about pairs nobody thought to write down. It is not
    automatically better about which pairs exist, and the glyph table demonstrates
    why: rendered in the generic grotesques available on a build machine, it ranks Q
    above O as the letter a 0 was, and drops I/J entirely, a confusion this project
    has actually observed in the field, where "GI 27 WR 8094" was a misread GJ.

    So the merge is asymmetric, and each rule earns its place.

    costs come from the overlay wherever it has an opinion. This is the part the
    prior genuinely cannot supply: a hand table has no principled way to say O to 0
    is a surer reading than J to 1, and every pair carrying the same weight is what
    made the split search in plates.py guess between them arbitrarily.

    coercion targets stay with the prior where it has one, because a wrong target is
    a wrong plate, and are taken from the overlay where the prior is silent, which
    is most of the alphabet. The prior mapped 15 letters; the glyph table maps every
    one of them.

    letter pairs are the union. Both sources find real confusions the other misses,
    and the uniqueness rule in correct_state is what keeps the extra pairs safe: a
    repair only happens when exactly one valid state code is reachable, so a
    spurious pair widens the search without licensing a guess.

    An overlay marked standalone skips all of this and replaces the prior outright.
    That is for the empirical table: once the substitutions have been counted on
    real reads with real ground truth, the prior is not a floor any more, it is
    out-of-date guesswork.
    """
    if overlay.standalone:
        return overlay

    to_digit = {**overlay.to_digit, **base.to_digit}
    to_letter = {**overlay.to_letter, **base.to_letter}
    costs = {**base.costs, **overlay.costs}
    return ConfusionModel(
        to_digit=to_digit,
        to_letter=to_letter,
        letter_pairs=base.letter_pairs | overlay.letter_pairs,
        costs=costs,
        source=f"{base.source}+{overlay.source}",
        provenance=overlay.provenance,
        standalone=False,
    )


def to_json(model_: ConfusionModel) -> dict:
    return {
        "source": model_.source,
        "provenance": model_.provenance,
        "standalone": model_.standalone,
        "to_digit": model_.to_digit,
        "to_letter": model_.to_letter,
        "letter_pairs": sorted(model_.letter_pairs),
        "costs": model_.costs,
    }


def from_json(payload: dict) -> ConfusionModel:
    return ConfusionModel(
        to_digit=dict(payload.get("to_digit") or {}),
        to_letter=dict(payload.get("to_letter") or {}),
        letter_pairs={"".join(sorted(p)) for p in payload.get("letter_pairs") or []},
        costs={k: float(v) for k, v in (payload.get("costs") or {}).items()},
        source=payload.get("source", "file"),
        provenance=payload.get("provenance", ""),
        standalone=bool(payload.get("standalone", False)),
    )


@lru_cache(maxsize=1)
def model(path: str | None = None) -> ConfusionModel:
    """The confusion model in force. Loaded once; falls back to the prior."""
    target = Path(path) if path else CONFUSION_PATH
    try:
        with open(target, encoding="utf-8") as handle:
            loaded = from_json(json.load(handle))
    except FileNotFoundError:
        log.info("no confusion table at %s; using the built-in prior", target)
        return _prior()
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        log.warning("confusion table %s unreadable (%s); using the prior", target, exc)
        return _prior()

    if not loaded.to_digit or not loaded.to_letter:
        log.warning("confusion table %s is incomplete; using the prior", target)
        return _prior()

    merged = merge(_prior(), loaded)
    log.info(
        "confusion model: %s (%s) — %d digit coercions, %d letter coercions, %d pairs",
        merged.source, merged.provenance or "no provenance recorded",
        len(merged.to_digit), len(merged.to_letter), len(merged.letter_pairs),
    )
    return merged


def entropy_weight(count: int, total: int) -> float:
    """-log likelihood, for turning empirical counts into a cost. Used by the
    learner; kept here so the cost scale is defined in one module."""
    if total <= 0 or count <= 0:
        return COST_IMPLAUSIBLE
    return min(COST_IMPLAUSIBLE, -math.log(count / total))
