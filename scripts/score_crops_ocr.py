"""Read folders of plate crops with the production Tesseract reader and score them.

For comparing image treatments (super-resolution, for one) on the hand-verified
test plates, crop against crop, with the same reader the platform runs. Each
folder holds `<sighting_id>.png`; the original corpus crops are always scored
alongside as the baseline, over exactly the same plates.

Tesseract lives in the ANPR image, not the host, so run it there:

    docker run --rm --entrypoint python -e PYTHONPATH=/app:/usr/lib/python3/dist-packages \\
      -v "$PWD/scripts:/app/scripts:ro" -v "$PWD/data/corpus:/corpus:ro" -v <runs>:/runs \\
      cctv-anpr -m scripts.score_crops_ocr --corpus /corpus --treated sr=/runs/<run>/test_sr

"Confidently wrong" counts reads that pass the Indian plate format check and are
still wrong. That is the failure an operator believes, and the one a treatment
that invents characters would increase while leaving edit distance flat.
"""

from __future__ import annotations

import argparse
import json
import pathlib

import cv2

from scripts.review_server import verified_plates
from services.anpr.backends.tesseract import TesseractPlateOcr
from services.common.plates import edit_distance, is_valid_format, normalise_plate


def score(reads: dict[str, str], truth: dict[str, str]) -> dict:
    stored = {k: normalise_plate(v) for k, v in reads.items()}
    d = [edit_distance(stored[k], t) for k, t in truth.items()]
    return {
        "n": len(d),
        "raw_exact": sum(reads[k] == t for k, t in truth.items()),
        "stored_exact": sum(x == 0 for x in d),
        "stored_within_1": sum(x <= 1 for x in d),
        "stored_within_2": sum(x <= 2 for x in d),
        "stored_mean_edit": round(sum(d) / len(d), 2),
        "confidently_wrong": sum(is_valid_format(reads[k]) and stored[k] != t
                                 for k, t in truth.items()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=pathlib.Path, default=pathlib.Path("data/corpus"))
    parser.add_argument("--treated", action="append", default=[], metavar="NAME=DIR",
                        help="a folder of treated crops named <sighting_id>.png")
    parser.add_argument("--out", type=pathlib.Path, help="write the full comparison as JSON")
    args = parser.parse_args()

    plates = verified_plates(args.corpus)
    truth = {r["sighting_id"]: r["truth"] for r in plates}
    folders = {"original": {r["sighting_id"]: args.corpus / r["image"] for r in plates}}
    for spec in args.treated:
        name, folder = spec.split("=", 1)
        folders[name] = {sid: pathlib.Path(folder) / f"{sid}.png" for sid in truth}

    ocr = TesseractPlateOcr()
    reads: dict[str, dict[str, str]] = {}
    for name, paths in folders.items():
        reads[name] = {}
        for sid, path in paths.items():
            # Everything goes through grey and back, originals included. Tesseract
            # on these crops changed 5 -> 4 exact from the colour conversion alone
            # (14 Sep), so two treatments differing there differ in noise.
            grey = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            result = ocr.read(cv2.cvtColor(grey, cv2.COLOR_GRAY2BGR))
            reads[name][sid] = (result.text if result else "").replace(" ", "").upper()

    report = {"plates": sorted(truth), "scores": {n: score(r, truth) for n, r in reads.items()}}
    base = {k: edit_distance(normalise_plate(v), truth[k]) for k, v in reads["original"].items()}
    for name in folders:
        if name == "original":
            continue
        mine = {k: edit_distance(normalise_plate(v), truth[k]) for k, v in reads[name].items()}
        report["scores"][name]["vs_original"] = {
            "better": sum(mine[k] < base[k] for k in truth),
            "worse": sum(mine[k] > base[k] for k in truth),
            "same": sum(mine[k] == base[k] for k in truth),
        }
    for name, s in report["scores"].items():
        print(f"{name:<12} {json.dumps(s)}")
    if args.out:
        report["reads"] = reads
        report["truth"] = truth
        args.out.write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
