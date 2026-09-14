"""Positional plate normalisation for Indian registration marks.

Invariant 3: normalisation is *positional* and is applied at both
write time and query time. A naive global map (``O -> 0`` everywhere) collapses
genuinely different plates onto the same key — ``GJ01OO1234`` and ``GJ0100 1234``
are different vehicles. We therefore decide each character's target class from
the slot it occupies in the Indian plate grammar, then coerce only within that
slot.

Grammar (BS / "modern" format)::

    GJ    01     AB      1234
    ^^    ^^     ^^      ^^^^
    state RTO    series  number
    2 A   1-2 D  0-3 A   4 D

Also handled: the Bharat (BH) series ``22 BH 1234 AA``.

Every other module must call :func:`normalise_plate` — never re-implement a
variant of this logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from services.common.confusion import model

__all__ = [
    "NormalisedPlate",
    "normalise_plate",
    "normalise",
    "is_valid_format",
    "edit_distance",
    "plate_key_variants",
    "correct_state",
]

# --- OCR confusion -----------------------------------------------------
# Which characters OCR mixes up, and how much each confusion costs, come from
# `services/common/confusion.py` — derived from glyph similarity or from
# measured substitutions, never typed in here. Applied ONLY inside a slot whose
# class is already known: a global map (`O -> 0` everywhere) collapses
# genuinely different plates onto one key, which is the failure this whole
# module exists to prevent.

# Junk the OCR routinely reads off the plate furniture itself.
_STRIP_TOKENS = ("IND", "BHARAT")

_CLEAN_RE = re.compile(r"[^A-Z0-9]")

# The canonical shape, checked *after* normalisation.
_BS_RE = re.compile(r"^[A-Z]{2}[0-9]{1,2}[A-Z]{0,3}[0-9]{4}$")
_BH_RE = re.compile(r"^[0-9]{2}BH[0-9]{4}[A-Z]{1,2}$")

# Valid Indian state / UT registration prefixes. Used as a tie-break hint only:
# an unknown prefix is never a reason to discard a read (see invariant on
# format-validation failures being kept, not dropped).
# Written as one block and split rather than as a list literal: it is read as a
# reference table far more often than it is edited, and a 38-element list of
# two-character strings is noise.
_STATE_CODES = frozenset(
    """AN AP AR AS BR CG CH DD DL DN GA GJ HP HR JH JK KA KL LA LD MH ML MN MP
    MZ NL OD OR PB PY RJ SK TN TR TS UK UA UP WB""".split()  # noqa: SIM905
)

_MIN_LEN = 7  # GJ 1 1234
_MAX_LEN = 11  # GJ 01 ABC 1234


@dataclass(frozen=True)
class NormalisedPlate:
    """Result of normalising one OCR string.

    `normalised` is the join key written to sightings.plate_normalised and used for
    every lookup. A false `format_valid` does not mean discard: the read is
    persisted at reduced confidence and flagged.
    """

    raw: str
    normalised: str
    format_valid: bool
    state: str | None = None
    rto: str | None = None
    series: str | None = None
    number: str | None = None
    #: True when the state code was repaired against the closed list of valid
    #: codes. The read is still trustworthy but it is not what OCR returned, and
    #: an operator comparing a sighting against a photograph should know that.
    state_corrected: bool = False

    @property
    def pretty(self) -> str:
        """Human display form, e.g. ``GJ 01 AB 1234``."""
        if not self.format_valid or self.state is None:
            return self.normalised
        parts = [self.state, self.rto or "", self.series or "", self.number or ""]
        return " ".join(p for p in parts if p)


def _coerce_digits(s: str) -> str:
    confusion = model()
    return "".join(confusion.coerce_to_digit(c) for c in s)


def _coerce_letters(s: str) -> str:
    confusion = model()
    return "".join(confusion.coerce_to_letter(c) for c in s)


def _clean(raw: str) -> str:
    s = _CLEAN_RE.sub("", (raw or "").upper())
    # "IND" is embossed on the plate, not part of the mark. Only strip it as a
    # prefix — "INDORE"-style noise never appears mid-string in a real read.
    for token in _STRIP_TOKENS:
        if s.startswith(token) and len(s) - len(token) >= _MIN_LEN:
            s = s[len(token) :]
    return s


# Cost of resolving one character into its slot's class. The per-character
# figures come from the confusion model, which ranks them; the only number
# decided here is the structural prior below, because it is a fact about Indian
# RTO numbering rather than about OCR.

# Two-digit RTO codes dominate the real distribution. Worth less than a single
# coercion, so it breaks ties without ever overriding character evidence.
_PRIOR_ONE_DIGIT_RTO = 1.0


def _char_cost(ch: str, wants_digit: bool) -> float:
    return model().char_cost(ch, wants_digit)


def _split_cost(middle: str, n_digits: int) -> float:
    return sum(_char_cost(ch, i < n_digits) for i, ch in enumerate(middle))


def _split_middle(middle: str) -> tuple[str, str]:
    """Split the RTO/series block into (rto_digits, series_letters), coerced.

    ``middle`` is everything between the 2-char state code and the 4-char
    number, so its length pins down the candidate splits: 1-2 digits of RTO
    followed by 0-3 letters of series. Where more than one split fits, the one
    needing the fewest and cheapest coercions wins.
    """
    m = len(middle)
    best: tuple[float, int, str, str] | None = None  # (cost, prefers_one, rto, series)
    for n_digits in (1, 2):
        n_letters = m - n_digits
        if not 0 <= n_letters <= 3:
            continue
        rto = _coerce_digits(middle[:n_digits])
        # There is no RTO code 0 or 00. A split implying one is not a reading of
        # an ambiguous plate, it is the wrong split — this is what separates
        # "GJ 0 IAB 1234" (impossible) from "GJ 01 AB 1234" (the real plate).
        if rto.isdigit() and int(rto) == 0:
            continue
        cost = _split_cost(middle, n_digits)
        cand = (
            cost + (_PRIOR_ONE_DIGIT_RTO if n_digits == 1 else 0),
            n_digits,
            rto,
            _coerce_letters(middle[n_digits:]),
        )
        if best is None or cand < best:
            best = cand
    if best is None:
        # No split fits the grammar (or every split implies RTO 0). Keep the
        # characters rather than inventing structure; the caller flags this.
        return middle, ""
    return best[2], best[3]


def _confusable(a: str, b: str) -> bool:
    """True where OCR mixes these two letters up.

    Letter-to-letter, so positional coercion cannot help: both characters are
    already the right class for a state-code slot. The pairs come from the
    confusion model, so this is the same evidence the positional coercion uses
    rather than a second, separately-maintained opinion about the same question.
    """
    return model().confusable(a, b)


def correct_state(code: str) -> str | None:
    """Repair a state code OCR got visually wrong, or return None.

    `GI 27 WR 8094` is not a plate: there is no GI. It is `GJ`, misread — and a
    J read as an I is not something positional coercion can fix, because both
    are letters and the slot wants a letter. So the closed list of real state
    codes is used as the vocabulary: if exactly one valid code differs by a
    single visually-confusable character, that is the read.

    Uniqueness is the safety property. Where two valid codes are equally
    plausible the read is left alone and flagged, because guessing between two
    real states is worse than admitting the character was unreadable.
    """
    if code in _STATE_CODES:
        return code
    candidates = [
        valid
        for valid in _STATE_CODES
        if sum(a != b for a, b in zip(code, valid, strict=False)) == 1
        and all(_confusable(a, b) for a, b in zip(code, valid, strict=False))
    ]
    return candidates[0] if len(candidates) == 1 else None


@lru_cache(maxsize=100_000)
def normalise(raw: str) -> NormalisedPlate:
    """Normalise one raw OCR string into a canonical plate record.

    Cached: the alerting path normalises the same watchlist entries on every
    sighting, and the journey path normalises the query plate repeatedly.
    """
    cleaned = _clean(raw)

    if not cleaned:
        return NormalisedPlate(raw=raw, normalised="", format_valid=False)

    # --- Bharat series: 22 BH 1234 AA. Detected before the BS grammar because
    # its leading two characters are digits, not a state code.
    bh = _try_bh(raw, cleaned)
    if bh is not None:
        return bh

    if not _MIN_LEN <= len(cleaned) <= _MAX_LEN:
        # Outside the grammar entirely. Keep the read — a mis-read wanted
        # vehicle is worse than a noisy record — but coerce nothing, because we
        # have no slots to coerce within.
        return NormalisedPlate(raw=raw, normalised=cleaned, format_valid=False)

    state = _coerce_letters(cleaned[:2])
    number = _coerce_digits(cleaned[-4:])
    rto, series = _split_middle(cleaned[2:-4])

    # Repair only where there is letter evidence to repair *from*. A state slot
    # whose characters were both digits has already been guessed at once by the
    # positional coercion, and repairing that guess compounds it: the burnt-in
    # overlay `14-06-2026` coerces to `IA` and would then be "corrected" to
    # `LA`, turning camera furniture into a structurally valid Ladakh plate.
    # One genuine letter is enough to anchor it; none is not.
    if any(c.isalpha() for c in cleaned[:2]):
        corrected = correct_state(state)
        state_corrected = corrected is not None and corrected != state
        if corrected is not None:
            state = corrected
    else:
        state_corrected = False

    coerced = f"{state}{rto}{series}{number}"
    structural = bool(_BS_RE.match(coerced))

    if not structural:
        # Coercion did not yield a plate shape, so it only distorted the read.
        # Keep what OCR actually saw: it is the more useful key for the fuzzy
        # and trigram paths, and the flag tells downstream code to distrust it.
        return NormalisedPlate(raw=raw, normalised=cleaned, format_valid=False)

    return NormalisedPlate(
        raw=raw,
        normalised=coerced,
        # Structurally a plate, but an unrecognised state prefix still counts as
        # a failed validation — flagged, never dropped.
        format_valid=state in _STATE_CODES,
        state=state,
        rto=rto,
        series=series or None,
        number=number,
        state_corrected=state_corrected,
    )


def _try_bh(raw: str, cleaned: str) -> NormalisedPlate | None:
    """Bharat-series plate: ``YY BH NNNN XX`` (10-11 chars)."""
    if not 10 <= len(cleaned) <= 11:
        return None
    if _coerce_letters(cleaned[2:4]) != "BH":
        return None
    year = _coerce_digits(cleaned[:2])
    number = _coerce_digits(cleaned[4:8])
    series = _coerce_letters(cleaned[8:])
    normalised = f"{year}BH{number}{series}"
    return NormalisedPlate(
        raw=raw,
        normalised=normalised,
        format_valid=bool(_BH_RE.match(normalised)),
        state="BH",
        rto=year,
        series=series or None,
        number=number,
    )


def normalise_plate(raw: str) -> str:
    """Canonical normalised key for ``raw``. The only plate key in the system."""
    return normalise(raw).normalised


def is_valid_format(raw: str) -> bool:
    """True if ``raw`` normalises to a structurally valid Indian plate."""
    return normalise(raw).format_valid


def edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Levenshtein distance, short-circuited at ``cap``.

    Used by the M5 match tiers (``probable`` <= 1, ``possible`` <= 2). Returns
    ``cap + 1`` rather than the true distance once the cap is exceeded.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(
                min(
                    prev[j] + 1,
                    cur[j - 1] + 1,
                    prev[j - 1] + (ca != cb),
                )
            )
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return min(prev[-1], cap + 1)


def plate_key_variants(raw: str) -> list[str]:
    """Keys worth probing for ``raw``, most specific first.

    The cleaned form is included alongside the normalised form so a query for a
    plate whose normalisation differs from how it was stored still hits. Order
    matters: callers try these in sequence and stop at the first hit.
    """
    cleaned = _clean(raw)
    norm = normalise(raw).normalised
    out = [norm]
    if cleaned and cleaned != norm:
        out.append(cleaned)
    return out
