"""Derive the OCR confusion table instead of typing one in.

Two ways to answer "which characters does OCR mix up, and how badly".

glyphs. Render every character the way a plate renders it, degrade the image the
way a CCTV camera does, and measure which pairs become hard to tell apart.
Confusion is fundamentally a visual property and this measures it directly: no
ground truth needed, it works before a single camera is onboarded, and it is
reproducible, since the same font and the same degradation give the same table
on any machine.

pairs. Count the substitutions that actually happened, from aligned (what OCR
returned, what was really on the plate) pairs. Strictly better where there is
enough data, because it captures this engine's real errors on this font at these
resolutions rather than a proxy for them. It needs ground truth, which is why it
is the second mode rather than the first.

Both write data/ocr-confusion.json, which services/common/confusion.py loads.
Absent that file the built-in prior is used, so this script improves the system
rather than being required by it.

Usage:
    python -m scripts.learn_confusion glyphs
    python -m scripts.learn_confusion glyphs --font /path/to/plate.ttf --blur 1.4
    python -m scripts.learn_confusion pairs --input reads.json
    python -m scripts.learn_confusion glyphs --dry-run   # print, write nothing

reads.json is [{"read": "GJ21AB1234", "truth": "GJ27AB1234"}, ...].
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys
from collections import Counter
from datetime import UTC, datetime

from services.common.confusion import (
    CONFUSION_PATH,
    DIGITS,
    LETTERS,
    ConfusionModel,
    from_similarity,
    to_json,
)

log = logging.getLogger("learn-confusion")

ALPHABET = LETTERS + DIGITS

#: Fonts to try, in order, for the glyph comparison. Plate characters are a
#: bold, condensed, grotesque sans; these are the closest widely-available
#: faces. The chosen font is recorded in the output's provenance, because the
#: table is only reproducible if you know which one produced it.
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
)

#: Height, in pixels, that each glyph is degraded to before comparison.
#:
#: This number is the whole point of the exercise. Compared at full resolution
#: no two characters look alike and the table comes out empty; compared at the
#: size a plate character actually occupies in a CCTV frame at a useful
#: distance — a dozen pixels tall — `O` and `0`, `8` and `B`, `5` and `S`
#: become genuinely hard to separate, which is exactly the regime the OCR is
#: working in and exactly the confusions it makes.
DEGRADE_HEIGHT = 12

#: Gaussian blur applied before downscaling, in pixels of the rendered glyph.
#: Stands in for optics, motion and compression.
DEGRADE_BLUR = 1.2

#: Size each glyph is rendered at before degradation.
RENDER_HEIGHT = 64


def _pick_fonts(explicit: list[str] | None) -> list[str]:
    if explicit:
        return explicit
    found = [c for c in FONT_CANDIDATES if pathlib.Path(c).exists()]
    if not found:
        raise SystemExit(
            "no usable font found. Pass --font with a path to a bold sans TTF, or "
            f"install one of: {', '.join(FONT_CANDIDATES)}"
        )
    return found


def _render(font_path: str, blur: float, height: int) -> dict[str, list[float]]:
    """One degraded, size-normalised bitmap per character, as a flat vector.

    Each glyph is rendered, blurred, cropped to its own ink and resized to a
    common box. Cropping before resizing is what makes this a comparison of
    *shape*: without it `I` and `1` would differ mostly by how much white space
    surrounds them, which is not something an OCR engine sees.
    """
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    font = ImageFont.truetype(font_path, RENDER_HEIGHT)
    box = (RENDER_HEIGHT * 2, RENDER_HEIGHT * 2)
    # Wide enough for the widest glyph at this height; narrow ones are padded
    # rather than stretched into it.
    width = max(4, round(height * 1.1))

    vectors: dict[str, list[float]] = {}
    for ch in ALPHABET:
        canvas = Image.new("L", box, color=0)
        ImageDraw.Draw(canvas).text(
            (box[0] // 2, box[1] // 2), ch, fill=255, font=font, anchor="mm"
        )
        canvas = canvas.filter(ImageFilter.GaussianBlur(blur))
        bounds = canvas.getbbox()
        if bounds is None:
            continue
        glyph = canvas.crop(bounds)

        # Scaled to a common height with the **aspect ratio preserved**, then
        # centred in a fixed box. Stretching each glyph to fill the box instead
        # would erase the single most discriminating feature a character has at
        # this size — how wide it is. Without this, `I` resizes into a solid
        # block and comes out "similar" to every dense glyph on the sheet,
        # which is an artefact of the normalisation and not something any OCR
        # engine would ever confuse.
        scaled_width = max(1, round(glyph.width * height / glyph.height))
        glyph = glyph.resize((min(scaled_width, width), height), Image.BILINEAR)
        small = Image.new("L", (width, height), color=0)
        small.paste(glyph, ((width - glyph.width) // 2, 0))

        pixels = list(small.tobytes())
        peak = max(pixels) or 1
        vectors[ch] = [p / peak for p in pixels]
    return vectors


def _similarity(vectors: dict[str, list[float]]) -> dict[str, float]:
    """Pairwise visual similarity in [0, 1], for every ordered pair.

    Pearson correlation between the two bitmaps, rescaled into [0, 1].

    Mean absolute difference was tried first and is subtly wrong here: it
    rewards glyphs of similar *ink density* regardless of where the ink is, so
    a mid-density curved character comes out "similar" to every digit on the
    sheet. That produced `8 -> S`, `9 -> S`, `6 -> S` and `3 -> S` all at once
    — a signature of the metric rather than of the letterforms. Correlation
    subtracts each glyph's own mean before comparing, which removes exactly
    that bias and leaves the question being asked as "is the ink in the same
    places", which is the one an OCR engine is answering.

    Symmetric, even though OCR error rates are not — one of the things the
    `pairs` mode captures and this mode cannot.
    """
    centred: dict[str, tuple[list[float], float]] = {}
    for ch, vector in vectors.items():
        mean = sum(vector) / len(vector)
        deltas = [v - mean for v in vector]
        norm = sum(d * d for d in deltas) ** 0.5 or 1e-9
        centred[ch] = (deltas, norm)

    scores: dict[str, float] = {}
    for a, (da, na) in centred.items():
        for b, (db, nb) in centred.items():
            if a == b:
                continue
            correlation = sum(x * y for x, y in zip(da, db, strict=True)) / (na * nb)
            # Correlation runs [-1, 1]; the confusion module's bands expect
            # [0, 1], and a negative correlation is "not alike" either way.
            scores[a + b] = round(max(0.0, correlation), 4)
    return scores


def learn_from_glyphs(fonts: list[str], blur: float, height: int) -> ConfusionModel:
    """Similarity averaged over several faces.

    One font gives one font's quirks. DejaVu's zero and its `B` degrade to
    similar blobs at this size in a way that is not true of plate typography
    generally, and a table built from it alone would coerce `0` to `B` in
    letter slots — a real measurement of the wrong thing. Averaging over
    several grotesque faces keeps what they agree on, which is the part that
    is about character shapes rather than about a typeface.
    """
    totals: dict[str, float] = {}
    for font_path in fonts:
        scores = _similarity(_render(font_path, blur, height))
        for pair, score in scores.items():
            totals[pair] = totals.get(pair, 0.0) + score
    averaged = {pair: round(total / len(fonts), 4) for pair, total in totals.items()}

    names = ", ".join(pathlib.Path(f).name for f in fonts)
    return from_similarity(
        averaged,
        source="glyph",
        provenance=(
            f"glyph similarity averaged over {len(fonts)} faces ({names}), "
            f"blur={blur}, degraded to {height}px tall, "
            f"generated {datetime.now(UTC).date().isoformat()}"
        ),
    )


def _align(read: str, truth: str) -> list[tuple[str, str]]:
    """Character substitutions between two strings of equal length.

    Deliberately refuses unequal lengths rather than guessing an alignment: an
    insertion or deletion mis-aligns everything after it, and a table poisoned
    by phantom substitutions is worse than a smaller honest one.
    """
    if len(read) != len(truth):
        return []
    return [(r, t) for r, t in zip(read, truth, strict=True) if r != t]


#: Error rate at which a substitution is treated as maximally confusable.
#: A character misread one time in ten is thoroughly confusable with what it
#: was misread as; above that the distinction stops carrying information, so
#: the scale saturates rather than letting one pathological pair set the scale
#: for everything else.
#:
#: With `MIN_SIMILARITY = 0.55` in confusion.py this makes the effective
#: decision "kept if the substitution happens on at least 5.5% of that
#: character's appearances", which is a statement about the data rather than
#: about the worst offender in it.
RATE_AT_FULL_SIMILARITY = float(os.environ.get("OCR_PAIRS_RATE_FULL", "0.10"))

#: Times a substitution must have been observed before its rate is believed.
#: A rate is only as good as its denominator: `Z` read as `2` came in at 5.9%
#: on the 6 Sep corpus, which clears the band comfortably — off three
#: occurrences. Three is not evidence, and a rare character is exactly where a
#: rate-based rule is easiest to fool.
MIN_SUPPORT = int(os.environ.get("OCR_PAIRS_MIN_SUPPORT", "10"))


def learn_from_pairs(path: pathlib.Path) -> ConfusionModel:
    payload = json.loads(path.read_text())
    substitutions: Counter[str] = Counter()
    occurrences: Counter[str] = Counter()
    aligned = 0
    skipped = 0

    for entry in payload:
        read = str(entry.get("read", "")).upper()
        truth = str(entry.get("truth", "")).upper()
        if not read or not truth:
            continue
        if len(read) != len(truth):
            skipped += 1
            continue
        aligned += 1
        for character in truth:
            occurrences[character] += 1
        for observed, actual in _align(read, truth):
            substitutions[observed + actual] += 1

    if not substitutions:
        raise SystemExit(
            f"{path}: no usable substitutions found in {aligned} aligned pairs "
            f"({skipped} skipped for differing length)"
        )

    # P(read `observed` | printed `actual`), mapped onto the similarity band by
    # an *absolute* calibration rather than by rescaling against the commonest
    # substitution.
    #
    # This used to divide every rate by the peak, and that was wrong in a way
    # that only shows up on real data. `MIN_SIMILARITY` in confusion.py is
    # 0.55, calibrated for *glyph* similarity — "how alike do these two
    # characters look". An error rate rescaled to a peak of 1.0 is a different
    # quantity on a different scale, and applying one threshold to both means
    # the single commonest error suppresses every other real one.
    #
    # Measured on the 775-pair corpus of 6 Sep 2026: `0` was read as `O` on
    # 35% of the 503 zeroes printed, which pinned the peak. `J` read as `I` —
    # 74 times, 11% of every J printed, unmistakably a real confusion — scored
    # 0.31 after rescaling and was discarded as not similar enough. The table
    # that came out had exactly one pair in it.
    rates = {
        pair: count / occurrences[pair[1]]
        for pair, count in substitutions.items()
        if occurrences.get(pair[1]) and count >= MIN_SUPPORT
    }
    if not rates:
        raise SystemExit(
            f"{path}: no substitution occurred at least {MIN_SUPPORT} times in "
            f"{aligned} aligned pairs. The prior stays the right table."
        )
    scores = {
        pair: round(min(1.0, rate / RATE_AT_FULL_SIMILARITY), 4)
        for pair, rate in rates.items()
    }
    _symmetrise_letters(scores)

    log.info(
        "%d aligned pairs, %d skipped for length, %d distinct substitutions, "
        "%d with support >= %d",
        aligned, skipped, len(substitutions), len(rates), MIN_SUPPORT,
    )
    return from_similarity(
        scores,
        source="empirical",
        provenance=(
            f"measured substitutions from {path.name}: {aligned} aligned reads, "
            f"{sum(substitutions.values())} substitutions, "
            f"generated {datetime.now(UTC).date().isoformat()}"
        ),
    )


def _symmetrise_letters(scores: dict[str, float]) -> None:
    """Mirror letter-to-letter scores, in place.

    `_mutual_letter_pairs` in confusion.py only accepts a letter pair when each
    letter is among the other's nearest — which is right for glyph similarity,
    where the measurement is symmetric by construction, and wrong here.

    A *measured* substitution is directional for a reason that has nothing to
    do with how alike the characters look: it depends on which characters
    appear on the plates that happened to drive past. On the 6 Sep corpus `J`
    was read as `I` 74 times and `I` was never read as `J` — not because the
    confusion runs one way, but because 679 J's went past and almost no I's
    did. Gujarat plates start `GJ`; they rarely contain an `I` at all.

    Confusability between two letters is a property of the pair. So a measured
    substitution in either direction is evidence for both, and without this the
    mutuality test discards every letter pair the corpus contains.

    Digits keep their measured direction, because the letter-to-digit coercion
    map *is* directional — it answers "what should this character be in a slot
    that must hold a digit", which is not a symmetric question.
    """
    for pair, score in list(scores.items()):
        if len(pair) != 2:
            continue
        a, b = pair[0], pair[1]
        if a in LETTERS and b in LETTERS and a != b:
            mirror = b + a
            scores[mirror] = max(scores.get(mirror, 0.0), score)


def _report(model: ConfusionModel) -> None:
    print(f"\nsource: {model.source}\nprovenance: {model.provenance}\n")
    print("letter -> digit")
    for letter in sorted(model.to_digit):
        digit = model.to_digit[letter]
        print(f"  {letter} -> {digit}   cost {model.costs.get(letter + digit, 0):.2f}")
    print("\ndigit -> letter")
    for digit in sorted(model.to_letter):
        letter = model.to_letter[digit]
        print(f"  {digit} -> {letter}   cost {model.costs.get(digit + letter, 0):.2f}")
    print(f"\nletter pairs ({len(model.letter_pairs)}): "
          f"{' '.join(sorted(model.letter_pairs))}\n")


def _would_lose_coverage(out: pathlib.Path, payload: dict) -> str | None:
    """Refuse a table that covers materially less than the one it replaces.

    The failure this prevents is quiet and expensive. `to_digit` and `to_letter`
    are the cross-class coercion maps that positional normalisation (invariant 3)
    is *made of*; empty them and normalisation silently stops repairing the
    errors it exists for, at both write and query time, and the plates already
    in the index no longer key the same way as new ones.

    Measured on 31 Aug 2026, which is why this exists: learning from 775 reads
    of this estate produced 1 cross-class pair against the glyph prior's 15, and
    1 cost against 234 — because the pipeline reads cleanly and the residual
    errors are too few and too uniform to derive a table from. Empirical is only
    better when there is enough evidence to *be* better; below that the prior
    wins, and the tool should say so rather than quietly degrading the platform.

    Returns a message when the write should be refused, or None to allow it.
    """
    if not out.exists():
        return None
    try:
        old = json.loads(out.read_text())
    except (OSError, ValueError):
        return None  # unreadable or absent: nothing to protect

    losses = []
    for key in ("to_digit", "to_letter", "costs", "letter_pairs"):
        before, after = len(old.get(key) or ()), len(payload.get(key) or ())
        if after < before:
            losses.append(f"{key} {before} -> {after}")
    if not losses:
        return None

    cross_before = len(old.get("to_digit") or ()) + len(old.get("to_letter") or ())
    cross_after = len(payload.get("to_digit") or ()) + len(payload.get("to_letter") or ())
    # A couple of pairs fewer is ordinary drift between two honest measurements.
    # Losing most of the cross-class map is not, and that is the one that breaks
    # normalisation rather than merely making it less confident.
    if cross_after >= cross_before or cross_before - cross_after <= 2:
        return None

    return (
        "the learned table covers less than the one it would replace: "
        + "; ".join(losses)
        + f". Cross-class coercion pairs {cross_before} -> {cross_after}, which is "
        "what positional normalisation is built from — emptying it stops plates "
        "being repaired at write and query time. Collect more corrupted reads "
        "(night and glare clips especially), or keep the glyph prior"
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
    parser = argparse.ArgumentParser(description="Derive the OCR confusion table.")
    parser.add_argument("mode", choices=("glyphs", "pairs"))
    parser.add_argument("--font", action="append",
                        help="TTF to render plate characters with; repeatable, "
                             "and the similarity is averaged over all of them")
    parser.add_argument("--blur", type=float, default=DEGRADE_BLUR)
    parser.add_argument("--height", type=int, default=DEGRADE_HEIGHT,
                        help="pixel height each glyph is degraded to before comparison")
    parser.add_argument("--input", type=pathlib.Path,
                        help="JSON list of {read, truth} pairs, for `pairs` mode")
    parser.add_argument("--out", type=pathlib.Path, default=CONFUSION_PATH)
    parser.add_argument("--dry-run", action="store_true", help="print, write nothing")
    parser.add_argument(
        "--force", action="store_true",
        help="write even if the new table covers materially less than the old one",
    )
    args = parser.parse_args()

    if args.mode == "glyphs":
        fonts = _pick_fonts(args.font)
        log.info("rendering %d characters in %d face(s): %s", len(ALPHABET),
                 len(fonts), ", ".join(pathlib.Path(f).name for f in fonts))
        model = learn_from_glyphs(fonts, args.blur, args.height)
    else:
        if not args.input:
            parser.error("`pairs` mode needs --input")
        model = learn_from_pairs(args.input)

    _report(model)

    if args.dry_run:
        return 0

    payload = to_json(model)
    if not args.force and (refusal := _would_lose_coverage(args.out, payload)):
        log.error("%s", refusal)
        log.error("refusing to overwrite %s — pass --force if this is intended", args.out)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    log.info("wrote %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
