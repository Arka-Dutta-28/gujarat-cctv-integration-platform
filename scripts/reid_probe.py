"""Does the appearance descriptor actually carry signal?

A re-identification feature is easy to ship and hard to justify: it always
returns something, ranked, and the ranking looks plausible whether or not the
descriptor means anything. So this measures it rather than demonstrating it.

The test is a natural experiment already present in the data. Two sightings
carrying the same plate are, overwhelmingly, the same vehicle; two carrying
different plates are, overwhelmingly, different vehicles. If the descriptor
carries signal, the first group must be measurably closer than the second. If
the two distributions overlap, the feature is decoration and should be described
as such.

It also counts the case the feature exists for: pairs whose plates differ by a
single character while their appearance is near-identical. Those are one vehicle
read twice with an OCR slip, which is what an operator would want surfaced and
what a plate-only index cannot connect.

Usage:
    python -m scripts.reid_probe --hours 6
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys

from services.common.config import settings

log = logging.getLogger("reid-probe")

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"

#: Pairs sampled per group. Enough for a stable median without asking the
#: database to compare every sighting with every other one.
SAMPLE = 4000

SAME_PLATE = """
SELECT (a.embedding <=> b.embedding) AS distance
  FROM sightings a
  JOIN sightings b
    ON b.plate_normalised = a.plate_normalised
   AND b.id > a.id
   AND b.camera_id <> a.camera_id
 WHERE a.ts > now() - make_interval(hours => %(hours)s)
   AND b.ts > now() - make_interval(hours => %(hours)s)
   AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
   AND a.identifying AND a.format_valid
 LIMIT %(sample)s
"""

DIFFERENT_PLATE = """
SELECT (a.embedding <=> b.embedding) AS distance
  FROM sightings a
  JOIN sightings b
    ON b.plate_normalised <> a.plate_normalised
   AND b.id > a.id
   AND b.camera_id <> a.camera_id
 WHERE a.ts > now() - make_interval(hours => %(hours)s)
   AND b.ts > now() - make_interval(hours => %(hours)s)
   AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
   AND a.identifying AND a.format_valid
 LIMIT %(sample)s
"""

#: Pairs one character apart whose vehicles look alike — the feature's reason
#: for existing. `levenshtein` comes from fuzzystrmatch, so it is computed here
#: rather than in SQL to avoid depending on an extension that may not be loaded.
NEAR_MISS = """
SELECT a.id AS a_id, b.id AS b_id, a.plate_normalised AS a_plate,
       b.plate_normalised AS b_plate, a.camera_id::text AS a_cam,
       b.camera_id::text AS b_cam, (a.embedding <=> b.embedding) AS distance
  FROM sightings a
  JOIN sightings b
    ON b.id > a.id
   AND b.camera_id <> a.camera_id
   AND char_length(b.plate_normalised) = char_length(a.plate_normalised)
   AND b.plate_normalised <> a.plate_normalised
 WHERE a.ts > now() - make_interval(hours => %(hours)s)
   AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
   AND a.identifying AND a.format_valid AND b.format_valid
   AND (a.embedding <=> b.embedding) < %(close)s
 LIMIT 2000
"""


def _distances(cur, sql: str, hours: int) -> list[float]:
    cur.execute(sql, {"hours": hours, "sample": SAMPLE})
    return [float(r[0]) for r in cur.fetchall()]


def _summarise(name: str, values: list[float]) -> dict:
    if not values:
        return {"name": name, "n": 0}
    ordered = sorted(values)
    return {
        "name": name,
        "n": len(ordered),
        "p10": ordered[int(0.10 * (len(ordered) - 1))],
        "median": statistics.median(ordered),
        "p90": ordered[int(0.90 * (len(ordered) - 1))],
    }


def main() -> int:
    import psycopg

    parser = argparse.ArgumentParser(description="Measure the re-ID descriptor.")
    parser.add_argument("--hours", type=int, default=6)
    parser.add_argument("--close", type=float, default=0.05,
                        help="Appearance distance counted as 'looks like the same vehicle'.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(f"\n{'=' * 74}\nVehicle re-identification — does the descriptor carry signal?"
          f"\n{'=' * 74}\n")

    with psycopg.connect(settings.dsn) as conn, conn.cursor() as cur:
        same = _summarise("same plate, different camera", _distances(cur, SAME_PLATE, args.hours))
        other = _summarise("different plates", _distances(cur, DIFFERENT_PLATE, args.hours))

        cur.execute(NEAR_MISS, {"hours": args.hours, "close": args.close})
        near = [r for r in cur.fetchall() if _one_apart(r[2], r[3])]

    if not same.get("n") or not other.get("n"):
        print(f"{RED}Not enough sightings with embeddings in the last "
              f"{args.hours}h to measure.{RESET}\n")
        return 1

    header = f"{'group':<32} {'pairs':>7} {'p10':>8} {'median':>8} {'p90':>8}"
    print(BOLD + header + RESET)
    print("-" * len(header))
    for group in (same, other):
        print(f"{group['name']:<32} {group['n']:>7} {group['p10']:>8.4f} "
              f"{group['median']:>8.4f} {group['p90']:>8.4f}")

    separation = other["median"] - same["median"]
    print("-" * len(header))
    verdict = GREEN if separation > 0.05 else RED
    print(
        f"\n{verdict}Median separation: {separation:.4f}{RESET} — sightings of the same "
        f"plate at different\ncameras are that much closer in appearance than sightings "
        f"of different plates."
    )
    print(
        f"{DIM}Both groups are noisy by construction: a plate read twice can be two "
        f"different\nvehicles when the read was wrong, and two different plates are "
        f"occasionally one\nvehicle whose plate was misread. The separation is the "
        f"signal despite that.{RESET}"
    )

    print(f"\n{BOLD}Near-misses the descriptor connects{RESET}")
    print(f"{DIM}Plates one character apart, at different cameras, whose vehicles look "
          f"nearly\nidentical (distance < {args.close}). Each is most likely one vehicle "
          f"read twice —\nwhich a plate-only index cannot connect.{RESET}\n")
    if not near:
        print("  none in this window\n")
    else:
        for row in near[:8]:
            print(f"  {row[2]:>11} ↔ {row[3]:<11} distance {float(row[6]):.4f} "
                  f"(sightings {row[0]}, {row[1]})")
        print(f"\n  {len(near)} such pairs in the last {args.hours}h\n")
    return 0


def _one_apart(a: str, b: str) -> bool:
    """Exactly one substitution apart. Same length is guaranteed by the query."""
    return sum(1 for x, y in zip(a, b, strict=True) if x != y) == 1


if __name__ == "__main__":
    sys.exit(main())
