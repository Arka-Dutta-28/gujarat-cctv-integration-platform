"""Contact sheets for verifying harvested plate crops by hand.

The harvest emits what the *pipeline* read. Training on that unchecked is a
confirmation loop — the model learns to reproduce its own mistakes with more
confidence. A human has to look, and this makes looking fast.

Why a contact sheet rather than a web tool: the images are 24-287 px wide and a
person can judge forty of them in a glance far quicker than clicking through
forty screens. The sheet prints the pipeline's read under each crop; the
reviewer only has to notice where it disagrees with the picture.

The first sheet from the government grid made the case for the whole exercise
better than any argument. Plates a person reads instantly — `GJ36AFD962`,
`GJ11DB7889` — came back as `GIIEAFOSE2` and `26511007889`. The image is legible
and the recogniser is not reading it; that is a model problem, not a camera one.

Workflow:
    python -m scripts.review_crops --out review/        # make the sheets + CSV
    ... open the sheets, correct the `truth` column in review/verify.csv ...
    python -m scripts.review_crops --apply review/verify.csv   # fold back in
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import pathlib
import sys
from datetime import datetime

log = logging.getLogger("review")

TILE_H = 64
PER_SHEET = 40
LABEL_H = 22

#: An Indian plate always carries a four-digit number and at least the two
#: letters of the state code. Used to *rank* candidates, never to discard them.
_DIGITS_IN_A_PLATE = 4


def plate_likeness(read: str, format_valid: bool) -> int:
    """How much this read looks like a plate. Higher is more promising.

    This exists because the first version of the review sheets sorted by crop width,
    which is exactly backwards. A burnt-in banner or a shop sign is physically far
    wider in the frame than a number plate seen at distance, so sorting by size
    guaranteed the first sheet was almost entirely furniture: CSITMS, SHOWROOM,
    ADVERTISE HERE, and the camera's own clock. A reviewer opening sheet 0 saw junk
    and reasonably concluded the harvest was broken. It was not; the ordering was.

    Measured over the 662-crop government corpus:

        rule                          kept   real plates   precision
        >= 4 digits                    150      43 / 51        28%
        >= 4 digits AND >= 1 letter     17      16 / 51        94%

    Which is why this ranks rather than filters. The strict rule is 94% precise and
    finds only a third of the plates, because OCR frequently mangles the state
    letters into digits, so 5199890737 is a plate and a hard filter would bin it.
    Ranking puts the 94% band first and leaves the rest reviewable.
    """
    digits = sum(c.isdigit() for c in read)
    letters = sum(c.isalpha() for c in read)
    if format_valid:
        return 4
    if digits >= _DIGITS_IN_A_PLATE and letters >= 1:
        return 3
    if digits >= _DIGITS_IN_A_PLATE:
        return 2  # probably a timestamp, but a mangled plate lands here too
    if digits:
        return 1
    return 0  # no digits at all — cannot be a plate. Words: signage, labels


#: A normalised Indian plate is 9 or 10 characters. Used only to order *within*
#: a band, never to judge across bands.
_PLATE_LENGTH = 10

#: What a reviewer writes when a plate is there and cannot be read. Distinct
#: from a blank, which means "this is not a plate at all".
UNREADABLE = "?"

#: What a reviewer writes for burnt-in furniture — a camera label, a clock, a
#: shop sign. Explicit rather than blank, because blank now means "not looked
#: at yet" and the two must never be confused.
NOT_A_PLATE = "x"


def _looks_like_a_clock(read: str) -> bool:
    """A digits-only read that is almost certainly the burnt-in date or time.

    One case survives the length ordering below: a date renders as ten digits, so
    13-06-2026 arrives as 1380622026, which is exactly plate length and sorted to
    the very top of the digits-only band.

    The year gives it away, and it is taken from the clock rather than written down,
    because a constant 2026 in here would quietly stop working in January and nobody
    would notice until the sheets looked wrong again.

    Only ever consulted inside the digits-only band. A read with letters in it is a
    plate candidate whatever digits it also contains, and GJ01AB2026 must not be
    demoted for having a year-shaped tail.
    """
    if any(c.isalpha() for c in read):
        return False
    year = str(datetime.now().year)
    return year in read or str(datetime.now().year - 1) in read


def _within_band(row: dict) -> tuple:
    """Order inside one band: clocks last, then closest to plate length, then widest.

    Width alone is wrong here for the same reason it was wrong across bands. In
    the digits-only band it is actively misleading: the widest entries are the
    burnt-in clock and date, while the real plates hiding in that band — the ones
    whose state letters OCR turned into digits — are narrower and were being
    pushed to the bottom of the last sheet.
    """
    return (
        _looks_like_a_clock(row["read"]),
        abs(len(row["read"]) - _PLATE_LENGTH),
        -row["plate_px"],
    )


#: Short names for filenames, so a sheet says what it holds before it is opened.
BAND_SLUG = {4: "plates", 3: "likely", 2: "digits-only", 1: "few-digits", 0: "no-digits"}

#: What each band means on the sheet, so a reviewer knows when to stop.
BAND_LABEL = {
    4: "looks like a valid plate",
    3: "digits and letters — most plates land here",
    2: "digits only — mostly clocks and dates, some mangled plates",
    1: "barely any digits — unlikely",
    0: "no digits — signage and camera labels, not plates",
}


def _banner(band: int, width: int):
    """A header saying what the text under each crop actually is.

    Without it the sheets read as captions — as though the string under a photo
    were a statement of what the plate says. It is the opposite: it is what the
    software *guessed*, and on this estate it is usually wrong. That mismatch is
    the entire point of the corpus, and twice it was reported as a bug in the
    sheets because nothing on the page said so.
    """
    import cv2
    import numpy as np

    lines = [
        ("The text under each crop is WHAT THE SOFTWARE READ.", (120, 200, 255)),
        ("It is usually WRONG - that is why we collect these.", (170, 170, 190)),
        ("Read the plate yourself; correct it in verify.csv.", (140, 220, 150)),
        (f"band {band}: {BAND_LABEL[band]}", (130, 130, 150)),
    ]
    # Fit to the sheet, which is only as wide as its widest crop. A banner that
    # runs off the edge is worse than no banner: the reader sees half a sentence
    # and learns nothing from it.
    usable = max(80, width - 16)
    scale = min(
        0.58,
        *(usable / max(1, cv2.getTextSize(t, cv2.FONT_HERSHEY_SIMPLEX, 0.58, 1)[0][0]) * 0.58
          for t, _ in lines),
    )
    step = int(20 * max(scale / 0.58, 0.55)) + 6
    strip = np.full((step * len(lines) + 10, width, 3), 18, np.uint8)
    for i, (text, colour) in enumerate(lines):
        cv2.putText(strip, text, (8, step * (i + 1)),
                    cv2.FONT_HERSHEY_SIMPLEX, scale, colour, 1)
    return strip


def _load(corpus: pathlib.Path) -> list[dict]:
    lines = (corpus / "manifest.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines if line]


def build_sheets(corpus: pathlib.Path, out: pathlib.Path, minimum: int) -> int:
    import cv2
    import numpy as np

    rows = [r for r in _load(corpus) if len(r["read"]) >= minimum]
    for r in rows:
        r["_band"] = plate_likeness(r["read"], bool(r.get("format_valid")))
    rows.sort(key=lambda r: (-r["_band"], _within_band(r), r["read"]))
    out.mkdir(parents=True, exist_ok=True)

    # Sheets never straddle a band.
    #
    # They used to be cut every 40 crops regardless, so the plate band ran out
    # part-way down sheet 0 and the rest of that page filled with the camera's
    # clock. Opening the first sheet and finding timestamps in it reads as a
    # broken harvest even when the ordering above is correct — the page is the
    # unit somebody actually opens, so the page is where the boundary belongs.
    chunks: list[tuple[int, list[dict]]] = []
    for band in sorted({r["_band"] for r in rows}, reverse=True):
        in_band = [r for r in rows if r["_band"] == band]
        for start in range(0, len(in_band), PER_SHEET):
            chunks.append((band, in_band[start : start + PER_SHEET]))

    sheets = 0
    index_of = {id(r): i for i, r in enumerate(rows)}
    for band, chunk in chunks:
        tiles = []
        for r in chunk:
            img = cv2.imread(str(corpus / r["image"]))
            if img is None:
                continue
            scale = TILE_H / img.shape[0]
            img = cv2.resize(img, (max(1, int(img.shape[1] * scale)), TILE_H))
            tile = np.full((TILE_H + LABEL_H, max(300, img.shape[1]), 3), 28, np.uint8)
            tile[:TILE_H, : img.shape[1]] = img
            # Index, not the sighting id: a reviewer types this into the CSV and
            # a UUID is a transcription error waiting to happen.
            # Colour by band, so a reviewer can see at a glance where the
            # productive part of the sheet ends.
            colour = {4: (150, 255, 150), 3: (170, 230, 255), 2: (140, 190, 220)}.get(
                r["_band"], (120, 120, 140)
            )
            cv2.putText(tile, f"{index_of[id(r)]:>4}  software read: {r['read']}",
                        (3, TILE_H + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1)
            tiles.append(tile)
        if not tiles:
            continue
        tiles.insert(0, _banner(band, max(t.shape[1] for t in tiles)))
        width = max(t.shape[1] for t in tiles)
        tiles = [np.pad(t, ((0, 0), (0, width - t.shape[1]), (0, 0)), constant_values=28)
                 for t in tiles]
        # The band is in the filename, so the reviewer knows what a sheet holds
        # before opening it and can stop at the point it stops being worth it.
        name = f"sheet-{sheets:03d}-band{band}-{BAND_SLUG[band]}.png"
        cv2.imwrite(str(out / name), np.vstack(tiles))
        sheets += 1

    with (out / "verify.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["index", "sighting_id", "read", "truth", "note"])
        for i, r in enumerate(rows):  # same order as the sheets
            # `truth` starts EMPTY, and empty means "not reviewed".
            #
            # It used to be pre-filled with the pipeline's read, so a reviewer
            # could edit only what was wrong. That was a trap: `--apply` then
            # certified every row in the file, including the hundreds nobody had
            # looked at, and the corpus came back claiming 139 verified plates
            # from 25 actual reviews — with `SHOWROOM` and `VIDHYA` among them.
            # That is exactly the confirmation loop this whole step exists to
            # prevent, and it was silent.
            #
            # Empty-means-unreviewed makes the safe state the default one. The
            # `read` column beside it still saves the typing where the pipeline
            # happened to be right.
            writer.writerow([i, r["sighting_id"], r["read"], "", ""])
        # Conventions, written where the reviewer will actually see them.
        writer.writerow([])
        writer.writerow(["# truth column:", "blank = NOT reviewed",
                         f"{NOT_A_PLATE} = not a plate",
                         f"{UNREADABLE} = a plate, but unreadable",
                         "anything else = the correct plate"])

    counts: dict[int, int] = {}
    for r in rows:
        counts[r["_band"]] = counts.get(r["_band"], 0) + 1
    log.info("%d sheets and verify.csv for %d crops in %s", sheets, len(rows), out)
    for band in sorted(counts, reverse=True):
        log.info("  band %d — %-52s %4d", band, BAND_LABEL[band], counts[band])
    log.info("Sheets are ordered most-plate-like first. The lower bands are")
    log.info("mostly burnt-in furniture; stop reviewing when they start.")
    log.info("Correct the `truth` column. Blank it for anything that is not a plate.")
    return sheets


def apply_verification(corpus: pathlib.Path, csv_path: pathlib.Path) -> int:
    """Fold reviewed truth back into the manifest. Only marks what a human saw."""
    by_id: dict[str, tuple[str, str]] = {}
    with csv_path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            # Upper-cased for plates, because a plate is upper case — but the
            # markers must be matched case-insensitively too, or `x` becomes `X`
            # and stops being a marker. That silently re-classified 81 pieces of
            # furniture as verified plates on the first run.
            by_id[row["sighting_id"]] = (row["truth"].strip().upper(), row.get("note", ""))

    rows = _load(corpus)
    verified = dropped = unreadable = 0
    for r in rows:
        entry = by_id.get(r["sighting_id"])
        if entry is None:
            continue
        truth, note = entry
        if not truth:
            continue  # not reviewed — leave it alone
        if truth == UNREADABLE:
            # There *is* a plate here and no human could read it. Excluded from
            # training like the furniture, but it means something completely
            # different and the two must not be merged: "the camera cannot
            # resolve this plate" is a statement about the camera, and it is the
            # evidence behind the coverage map. Collapsing it into "not a plate"
            # would quietly overstate how much of the estate is readable.
            r["verified"], r["truth"] = True, None
            r["not_a_plate"], r["unreadable"] = False, True
            unreadable += 1
        elif truth == NOT_A_PLATE.upper():
            # Not a plate — usually burnt-in furniture. Kept in the manifest and
            # marked, rather than deleted: "the pipeline read the camera's own
            # label here" is a useful negative, and silently dropping rows makes
            # the corpus impossible to audit.
            r["verified"], r["truth"] = True, None
            r["not_a_plate"], r["unreadable"] = True, False
            dropped += 1
        else:
            r["verified"], r["truth"] = True, truth
            r["not_a_plate"], r["unreadable"] = False, False
            verified += 1
        if note:
            r["note"] = note

    (corpus / "manifest.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    log.info(
        "%d verified as plates, %d not a plate, %d present but unreadable, "
        "%d still unreviewed",
        verified, dropped, unreadable, sum(1 for r in rows if not r.get("verified")),
    )
    return verified


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
    parser = argparse.ArgumentParser(description="Verify harvested crops by hand.")
    parser.add_argument("--corpus", type=pathlib.Path, default=pathlib.Path("data/corpus"))
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("data/corpus/review"))
    parser.add_argument("--min-chars", type=int, default=6,
                        help="skip very short reads; they are almost all furniture")
    parser.add_argument("--apply", type=pathlib.Path,
                        help="fold a completed verify.csv back into the manifest")
    args = parser.parse_args()

    if not (args.corpus / "manifest.jsonl").is_file():
        log.error("no manifest at %s — run scripts.harvest_plate_crops first", args.corpus)
        return 1
    if args.apply:
        apply_verification(args.corpus, args.apply)
        return 0
    build_sheets(args.corpus, args.out, args.min_chars)
    return 0


if __name__ == "__main__":
    sys.exit(main())
