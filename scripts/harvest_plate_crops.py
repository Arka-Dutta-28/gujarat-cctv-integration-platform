"""Harvest plate crops from sightings the platform has already recorded.

Stage 2 of the recogniser training plan: real images from the government grid, to
anchor synthetic training data to what the cameras actually produce.

Why this is possible at all. The platform already keeps one evidence JPEG per
sighting, because the alert must arrive "with crop, camera, time and map pin
attached" and the detection report is specified down to the thumbnail. The raw
material therefore exists without running anything new: 28,039 sightings
currently carry a crop, a vehicle box, a plate box and a read.

The geometry is the fiddly part. Three coordinate spaces have to be reconciled,
and getting it wrong yields crops that look plausible and are cut in the wrong
place:

1. bbox and plate_bbox are both in frame coordinates.
2. The stored JPEG is the vehicle crop, so its origin is bbox's top left.
3. That JPEG may have been downscaled on write, if the vehicle box was larger
   than the crop module's longest-edge cap. The scale is recoverable as stored
   width divided by vehicle-box width.

One further detail would quietly poison a training set: the stored crop has the
plate box drawn on it, a 2 px rectangle, deliberately, because a reviewer needs
to see that the pipeline boxed a plate rather than a bumper sticker. Those drawn
pixels are not plate, so every crop here is inset past them.

Purpose binding. These are real vehicles on real public roads, and a
registration number is personal data. The platform's position (docs/hld.md
section 7.7) is that access is purpose-bound, so a corpus built for model
training records that purpose rather than inheriting the evidence retention it
was stored under. The harvest writes a PURPOSE.md into the corpus directory,
records a report.export class entry in the audit trail, writes the corpus
outside the served tree and gitignores it, and carries an explicit delete-after
date.

Usage:
    python -m scripts.harvest_plate_crops --crop-root /data/crops --out data/corpus
    python -m scripts.harvest_plate_crops --dry-run

The crops live in a Docker volume, so this normally runs inside the ANPR image.
Pass --user or the corpus lands owned by root and the review step cannot write
into it:

    docker run --rm --user "$(id -u):$(id -g)" \
      -v cctv_anpr-crops:/data/crops:ro -v "$PWD":/w -w /w \
      -e PYTHONPATH=/app:/usr/lib/python3/dist-packages \
      --network cctv_default cctv-anpr:latest \
      python -m scripts.harvest_plate_crops --only government
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from services.common.config import settings

log = logging.getLogger("harvest")

#: Pixels trimmed from each edge of the plate box before cropping.
#:
#: The stored evidence crop has a 2 px rectangle drawn around the plate. Three
#: pixels clears it with a margin for the JPEG ringing along that hard edge —
#: leaving it in would train the model that plates have a yellow border.
DRAWN_BOX_INSET_PX = 3

#: Narrower than this and there is nothing to learn from. Well below the 80 px
#: readability floor on purpose: crops in the 40-80 px band are exactly the hard
#: cases the fine-tune is *for*, and excluding them would train a model on the
#: easy half of the problem and then be surprised at night.
MIN_PLATE_PX = 24

#: Reads shorter than this cannot be a plate and are usually burnt-in furniture
#: — a camera label, part of a timestamp.
MIN_READ_CHARS = 4

#: How long the corpus may be kept. Written into PURPOSE.md and meant to be
#: honoured: a training corpus of real number plates is not evidence and has no
#: business outliving the training run.
RETENTION_DAYS = 90

_SELECT = """
SELECT s.id, s.ts, s.plate_raw, s.plate_normalised, s.confidence, s.condition,
       s.format_valid, s.bbox, s.plate_bbox, s.crop_path,
       c.external_ref, c.name AS camera_name
  FROM sightings s
  JOIN cameras c ON c.id = s.camera_id
 WHERE s.crop_path IS NOT NULL
   AND s.bbox IS NOT NULL
   AND s.plate_bbox IS NOT NULL
   AND length(s.plate_raw) >= %(min_chars)s
   -- Cast explicitly: with the parameter used only inside IS NULL and
   -- LIKE, Postgres cannot infer a type and rejects the statement.
   AND (%(ref_prefix)s::text IS NULL OR c.external_ref LIKE %(ref_prefix)s::text)
 ORDER BY s.ts DESC
 LIMIT %(limit)s
"""


def plate_region(
    bbox: list[int], plate_bbox: list[int], stored_w: int, stored_h: int
) -> tuple[int, int, int, int] | None:
    """The plate's box within the *stored* JPEG, inset past the drawn rectangle.

    Returns None when the result would be empty or degenerate — which happens
    for a plate box on the very edge of the vehicle box, where the inset eats
    the whole region.
    """
    vx1, vy1, vx2, vy2 = bbox
    vehicle_w, vehicle_h = vx2 - vx1, vy2 - vy1
    if vehicle_w <= 0 or vehicle_h <= 0:
        return None

    # The crop module scales by the longest edge, so both axes share one factor.
    scale = stored_w / vehicle_w

    px1, py1, px2, py2 = plate_bbox
    x1 = int(round((px1 - vx1) * scale)) + DRAWN_BOX_INSET_PX
    y1 = int(round((py1 - vy1) * scale)) + DRAWN_BOX_INSET_PX
    x2 = int(round((px2 - vx1) * scale)) - DRAWN_BOX_INSET_PX
    y2 = int(round((py2 - vy1) * scale)) - DRAWN_BOX_INSET_PX

    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(stored_w, x2), min(stored_h, y2)
    if x2 - x1 < MIN_PLATE_PX or y2 - y1 < 4:
        return None
    return x1, y1, x2, y2


def _rows(limit: int, ref_prefix: str | None) -> list[dict[str, Any]]:
    with (
        psycopg.connect(settings.dsn, row_factory=psycopg.rows.dict_row) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            _SELECT,
            {"limit": limit, "min_chars": MIN_READ_CHARS, "ref_prefix": ref_prefix},
        )
        return cur.fetchall()


def _purpose_note(out: pathlib.Path, kept: int, source: str) -> None:
    """Say what this corpus is for, and when it must be gone."""
    expires = (datetime.now(UTC) + timedelta(days=RETENTION_DAYS)).date()
    (out / "PURPOSE.md").write_text(
        f"""# Training corpus — purpose record

**Created:** {datetime.now(UTC).date()}
**Delete by:** {expires} ({RETENTION_DAYS} days)
**Images:** {kept}
**Source:** {source}

## What this is

Plate crops extracted from sightings this platform recorded, for the single
purpose of **fine-tuning the plate recogniser**.

## Why this file exists

These are registration numbers of real vehicles on real public roads, which is
personal data. The platform's stated position (`docs/hld.md` §7.7) is that
access to it is purpose-bound — a case reference on every query. The crops were
retained under a different purpose: evidence for alerts and detection reports.
Building a training corpus is a **new purpose**, and stating it is the whole
point of purpose binding.

## Constraints this corpus is held under

- Training only. Not for identifying any vehicle or person, not for any query.
- Not served by the API, not copied outside this machine, and gitignored.
- Deleted on or before the date above. A training corpus is not evidence and
  has no business outliving the training run.
- If the model is published, this corpus is not.
""",
        encoding="utf-8",
    )


#: Fields a human put there. A re-harvest must carry them across.
VERIFICATION_FIELDS = ("verified", "truth", "not_a_plate", "unreadable", "note", "disputed")


def carry_verifications(out: pathlib.Path, manifest: list[dict[str, Any]]) -> int:
    """Keep human verdicts when re-harvesting over an existing corpus.

    The harvest rebuilds the manifest from the database, so without this a
    second run silently discards every verification in it — and a re-harvest is
    exactly what happens when new footage arrives, which is precisely when the
    existing labels are most valuable.

    `labels.jsonl` would still hold them, and `review_server --apply` would put
    them back. But relying on that means the manifest is briefly wrong in a way
    nothing announces, and the reviewer who opens it in between sees 141 crops
    they finished sitting there as unlabelled. Merge here instead, so the file
    on disk is never the misleading one.
    """
    path = out / "manifest.jsonl"
    if not path.is_file():
        return 0
    previous = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            previous[row["sighting_id"]] = row
    carried = 0
    for row in manifest:
        old = previous.get(row["sighting_id"])
        if not old or not old.get("verified"):
            continue
        for field in VERIFICATION_FIELDS:
            if field in old:
                row[field] = old[field]
        carried += 1
    return carried


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
    parser = argparse.ArgumentParser(description="Harvest plate crops for fine-tuning.")
    parser.add_argument("--crop-root", type=pathlib.Path, default=pathlib.Path("/data/crops"))
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("data/corpus"))
    parser.add_argument("--limit", type=int, default=50_000)
    parser.add_argument(
        "--only", choices=("government", "simulated", "all"), default="all",
        help="government = the real grid only, which is the point of stage 2",
    )
    parser.add_argument("--dry-run", action="store_true", help="count, write nothing")
    args = parser.parse_args()

    prefix = {"government": "sentinel-cam%", "simulated": "cam-%", "all": None}[args.only]
    rows = _rows(args.limit, prefix)
    log.info("%d candidate sightings", len(rows))
    if not rows:
        log.error("nothing to harvest — is the crop volume mounted and the estate running?")
        return 1

    import cv2

    out = args.out
    if not args.dry_run:
        (out / "images").mkdir(parents=True, exist_ok=True)

    manifest: list[dict[str, Any]] = []
    skipped = {"missing_file": 0, "unreadable": 0, "geometry": 0}

    for row in rows:
        path = args.crop_root / row["crop_path"]
        if not path.is_file():
            skipped["missing_file"] += 1
            continue
        image = cv2.imread(str(path))
        if image is None:
            skipped["unreadable"] += 1
            continue

        region = plate_region(row["bbox"], row["plate_bbox"], image.shape[1], image.shape[0])
        if region is None:
            skipped["geometry"] += 1
            continue

        x1, y1, x2, y2 = region
        plate = image[y1:y2, x1:x2]
        name = f"{row['id']}.png"
        if not args.dry_run:
            # PNG, not JPEG: these are already JPEG-compressed once, and a second
            # lossy pass would add artefacts the camera never produced and teach
            # the model to expect them.
            cv2.imwrite(str(out / "images" / name), plate)

        manifest.append({
            "image": f"images/{name}",
            "read": row["plate_raw"],
            "normalised": row["plate_normalised"],
            "confidence": round(float(row["confidence"]), 4),
            "condition": row["condition"],
            "format_valid": row["format_valid"],
            "camera": row["external_ref"],
            "camera_name": row["camera_name"],
            "sighting_id": str(row["id"]),
            "ts": row["ts"].isoformat(),
            "plate_px": x2 - x1,
            # Nothing here is ground truth yet. A human decides that, and until
            # one has, this stays false — see scripts/review_crops.py.
            "verified": False,
        })

    log.info(
        "%d crops extracted; skipped %d (missing file %d, unreadable %d, geometry %d)",
        len(manifest), sum(skipped.values()),
        skipped["missing_file"], skipped["unreadable"], skipped["geometry"],
    )
    if manifest:
        widths = sorted(m["plate_px"] for m in manifest)
        log.info(
            "plate width px: min %d, median %d, max %d",
            widths[0], widths[len(widths) // 2], widths[-1],
        )
        by_condition: dict[str, int] = {}
        for m in manifest:
            key = m["condition"] or "unknown"
            by_condition[key] = by_condition.get(key, 0) + 1
        log.info("by condition: %s", by_condition)

    if args.dry_run:
        log.info("dry run — nothing written")
        return 0

    carried = carry_verifications(out, manifest)
    if carried:
        log.info("carried %d existing verifications across the re-harvest", carried)

    (out / "manifest.jsonl").write_text(
        "".join(json.dumps(m) + "\n" for m in manifest), encoding="utf-8"
    )
    _purpose_note(out, len(manifest), args.only)
    log.info("wrote %s (manifest.jsonl, images/, PURPOSE.md)", out)
    log.warning(
        "these are UNVERIFIED pipeline reads, not ground truth. Verify with "
        "`python -m scripts.review_crops` before training on them."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
