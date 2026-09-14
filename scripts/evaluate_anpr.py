"""Measure ANPR accuracy against the ground truth in the generated clips.

The honesty convention requires a defensible accuracy figure rather than a
flattering one. This is what makes a defensible figure possible: the synthetic
clips were generated from a manifest recording exactly which plate crossed
which clip in which second, so a read can be scored rather than eyeballed.

What is measured, and what each number means:

    recall            of the vehicles that genuinely passed, how many produced
                      a sighting whose plate matched. The number an
                      investigator cares about, because a missed vehicle is a
                      missed vehicle.
    plate accuracy    of the sightings produced, how many were exactly right.
                      Distinct from recall: a pipeline can read every vehicle
                      and get half of them wrong.
    near misses       reads within edit distance 1 or 2 of a real plate.
                      Reported separately because they are not noise: the M5
                      match tiers exist to surface them, and a wanted vehicle
                      found at distance 1 is a result, not a failure.
    by condition      day, night and glare separately, never averaged. Three of
                      the four real feeds observed are night scenes with
                      headlight bloom, so a single figure would average two
                      different problems.

The figure is scoped to ANPR-grade cameras (--capable-only, on by default),
because 31 of the 81 cameras in this estate physically cannot resolve a plate:
their crops run 66 px across against the simulated farm's 276 px, roughly 7 px
per character. Averaging them in produces a number true of no camera in the
estate. The scope comes from /api/cameras/anpr-capability, which derives it from
measured crop sizes rather than from a camera naming convention. The naming
convention is what this script used to rely on, and it would silently exclude a
genuinely ANPR-grade government feed the moment one appeared.

Everything is read from the live index through the API; nothing is recomputed
from the pipeline's internals.

Usage:
    python -m scripts.evaluate_anpr --minutes 10
"""

from __future__ import annotations

import argparse
import json
import pathlib
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from services.common.plates import edit_distance, normalise_plate

DEFAULT_API = "http://localhost:8000"
DEFAULT_MANIFEST = pathlib.Path("data/test-videos/manifest.json")
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


@dataclass
class Score:
    condition: str
    truth_plates: set[str] = field(default_factory=set)
    read_plates: list[str] = field(default_factory=list)
    exact: int = 0
    distance_1: int = 0
    distance_2: int = 0
    wrong: int = 0
    confidences: list[float] = field(default_factory=list)

    @property
    def sightings(self) -> int:
        return len(self.read_plates)

    @property
    def plate_accuracy(self) -> float:
        return self.exact / self.sightings if self.sightings else 0.0

    @property
    def usable(self) -> float:
        """Exact plus near misses — what the M5 tiers would surface."""
        found = self.exact + self.distance_1 + self.distance_2
        return found / self.sightings if self.sightings else 0.0

    @property
    def mean_confidence(self) -> float:
        return sum(self.confidences) / len(self.confidences) if self.confidences else 0.0


def _get(url: str, timeout: float = 60.0):
    req = urllib.request.Request(url)
    req.add_header("X-Actor", "anpr-evaluation")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read() or "null")


#: How far a read may be from a ground-truth plate and still be attributed to
#: it. Two matches the `possible` match tier; beyond that the read is more
#: likely a different vehicle, or noise, than a corruption of this one.
MAX_ATTRIBUTION_DISTANCE = 2

#: Corrupted reads needed before a *learned* table is better than the glyph
#: prior. Below this, the counts are dominated by which handful of plates
#: happened to pass a camera, and the table would encode that accident as if it
#: were a property of the OCR engine.
MIN_CORRUPTED_TO_LEARN = 50


def truth_from_manifest(path: pathlib.Path) -> set[str]:
    """Every plate the generated clips ever show, normalised.

    Deliberately a set across all clips rather than per clip: the simulator
    assigns clips to cameras and seeks each to its own offset, so which plate
    should appear on which camera at which wall-clock second is not knowable
    from here without duplicating the simulator's arithmetic. Scoring against
    the whole vocabulary is the weaker, honest claim — it measures whether the
    pipeline reads plates correctly, not whether it attributes them to the right
    camera. Cross-camera attribution is what M4's journey test measures.
    """
    manifest = json.loads(path.read_text())
    return {
        normalise_plate(p["plate"])
        for clip in manifest.get("clips", [])
        for p in clip.get("passes", [])
    }


def capable_cameras(api: str) -> tuple[set[str], str]:
    """The cameras any accuracy figure is allowed to be quoted over.

    Read from the platform's own capability assessment rather than recomputed
    here, so the report and the API cannot disagree about which cameras can read
    a plate.
    """
    report = _get(f"{api}/api/cameras/anpr-capability?days=1")
    capable = {c["camera_id"] for c in report["cameras"] if c["counts_toward_accuracy"]}
    return capable, report["accuracy_scope"]


def attribute_with_reason(plate: str, truth: set[str]) -> tuple[str | None, str]:
    """`attribute`, but it also says why it refused.

    The reason matters because the failure it explains is otherwise mute. With
    two corrupted reads present and zero attributed, "0 corrupted" is equally
    consistent with attribution being broken and with attribution working
    exactly as designed — and those call for opposite responses. Counting the
    refusals by reason makes the difference visible in the run's own output.
    """
    if plate in truth:
        return plate, "exact"
    same_length = [t for t in truth if len(t) == len(plate)]
    if not same_length:
        # An insertion or deletion misaligns every character after it, so
        # position-wise substitution counts learned from it would be fiction.
        return None, "no truth plate of the same length"
    scored = sorted(edit_distance(plate, t) for t in same_length)
    best = scored[0]
    if best > MAX_ATTRIBUTION_DISTANCE:
        return None, f"nearest same-length plate is {best} edits away"
    if len(scored) > 1 and scored[1] == best:
        return None, f"tied between two plates at {best} edits"
    candidate = min(
        (t for t in same_length if edit_distance(plate, t) == best), key=str
    )
    return candidate, "attributed"


def attribute(plate: str, truth: set[str]) -> str | None:
    """The one ground-truth plate this read is unambiguously a corruption of.

    Returns None when nothing is close enough, when the closest is ambiguous
    (two truth plates tie), or when the lengths differ — an insertion or a
    deletion mis-aligns every character after it, and a confusion table poisoned
    by phantom substitutions is worse than a smaller honest one.

    Deliberately strict. This feeds `learn_confusion pairs`, whose output
    becomes the coercion map every plate in the system is normalised through
    (invariant 3); a wrong attribution here propagates into the join key at both
    write and query time.
    """
    return attribute_with_reason(plate, truth)[0]


def score(
    api: str, minutes: int, truth: set[str], prefix: str,
    capable: set[str] | None = None,
    pairs: list[dict[str, str]] | None = None,
    refusals: Counter[str] | None = None,
) -> dict[str, Score]:
    cameras = {
        c["id"]: c["external_ref"]
        for c in _get(f"{api}/api/cameras?limit=500")
        if (c.get("external_ref") or "").startswith(prefix)
        # Ground truth exists only for the generated clips, so the prefix stays.
        # Capability narrows it further, and is the check that survives the
        # arrival of real ANPR-grade feeds.
        and (capable is None or c["id"] in capable)
    }
    if not cameras:
        raise SystemExit(
            f"no ANPR-grade cameras with external_ref starting {prefix!r}"
            if capable is not None
            else f"no cameras with external_ref starting {prefix!r}"
        )

    since = f"now-{minutes}m"
    sightings = _get(
        f"{api}/api/sightings?limit=1000&since="
        + urllib.parse.quote(_iso_since(minutes))
    )
    sightings = [s for s in sightings if s["camera_id"] in cameras]

    if refusals is None:
        refusals = Counter()

    scores: dict[str, Score] = defaultdict(lambda: Score(condition="unknown"))
    for s in sightings:
        condition = s.get("condition") or "unknown"
        bucket = scores.setdefault(condition, Score(condition=condition))
        plate = s["plate_normalised"]
        bucket.read_plates.append(plate)
        bucket.confidences.append(s["confidence"])

        if pairs is not None:
            # The **raw** read, not the normalised one, and this is the whole
            # correctness of the exercise.
            #
            # `plate_normalised` has already had positional coercion applied
            # using the very table we are trying to learn. Every cross-class
            # error — a letter read in a digit slot — has therefore already been
            # repaired and is invisible here. Learning from it sees only the
            # same-class residue, produces empty `to_digit`/`to_letter` maps,
            # and writing that back deletes the coercion map that hid the errors
            # in the first place. Measured 31 Aug 2026: 788 reads yielded a
            # table with 0 cross-class pairs against the prior's 15.
            #
            # Scoring above still uses the normalised plate, which is correct —
            # that is what the platform actually stores and matches on.
            raw = s.get("plate_raw") or plate
            # Exact reads are collected too, and that is not redundant: the
            # learner divides substitution counts by how often each character
            # was *printed*, so omitting the correct reads would inflate every
            # rate by the pipeline's own accuracy.
            attributed, why = attribute_with_reason(raw, truth)
            if attributed is not None:
                pairs.append({"read": raw, "truth": attributed})
            else:
                refusals[why] += 1

        if plate in truth:
            bucket.exact += 1
            continue
        best = min((edit_distance(plate, t) for t in truth), default=9)
        if best == 1:
            bucket.distance_1 += 1
        elif best == 2:
            bucket.distance_2 += 1
        else:
            bucket.wrong += 1

    _ = since
    return dict(scores)


def _iso_since(minutes: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def render(
    scores: dict[str, Score], truth: set[str], minutes: int, scope: str | None = None
) -> None:
    print(f"\n{'=' * 78}\nANPR accuracy against generated ground truth\n{'=' * 78}\n")
    print(f"{DIM}Window: last {minutes} minutes · {len(truth)} distinct plates in the "
          f"ground-truth vocabulary{RESET}")
    # The scope is printed with the figure, not alongside it in a document. A
    # number quoted without its denominator is how an estate-wide accuracy claim
    # gets made by accident.
    print(f"{DIM}Scope: {scope}{RESET}\n" if scope else "")

    if not scores:
        print(f"{RED}No sightings from the simulated farm in this window.{RESET}\n")
        return

    header = f"{'condition':<10} {'sightings':>9} {'exact':>7} {'d=1':>5} {'d=2':>5} " \
             f"{'wrong':>6} {'accuracy':>9} {'usable':>7} {'conf':>6}"
    print(BOLD + header + RESET)
    print("-" * len(header))

    total = Score(condition="all")
    for condition in sorted(scores):
        s = scores[condition]
        total.read_plates.extend(s.read_plates)
        total.confidences.extend(s.confidences)
        total.exact += s.exact
        total.distance_1 += s.distance_1
        total.distance_2 += s.distance_2
        total.wrong += s.wrong
        print(
            f"{condition:<10} {s.sightings:>9} {s.exact:>7} {s.distance_1:>5} "
            f"{s.distance_2:>5} {s.wrong:>6} {s.plate_accuracy:>8.1%} "
            f"{s.usable:>6.1%} {s.mean_confidence:>6.2f}"
        )

    print("-" * len(header))
    print(
        f"{BOLD}{'all':<10} {total.sightings:>9} {total.exact:>7} {total.distance_1:>5} "
        f"{total.distance_2:>5} {total.wrong:>6} {total.plate_accuracy:>8.1%} "
        f"{total.usable:>6.1%} {total.mean_confidence:>6.2f}{RESET}"
    )

    print(
        f"\n{DIM}accuracy = exact matches / sightings. usable = exact + within edit "
        f"distance 2, which is what\nthe M5 match tiers surface to an operator. "
        f"Conditions are never averaged into one figure —\nsee docs/field-observations.md §4."
        f"{RESET}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Score ANPR against ground truth.")
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--manifest", type=pathlib.Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--minutes", type=int, default=10)
    parser.add_argument("--prefix", default="cam-", help="Scope to the simulated farm.")
    parser.add_argument(
        "--all-cameras", action="store_true",
        help="Include cameras that cannot resolve a plate. Produces a figure that "
             "is true of no camera in the estate; for diagnosis only.",
    )
    parser.add_argument(
        "--dump-pairs", type=pathlib.Path, metavar="PATH",
        help="Also write the (read, truth) pairs behind this score, for "
             "`python -m scripts.learn_confusion pairs --input PATH`. This is how "
             "the OCR confusion table stops being a glyph-similarity guess and "
             "becomes a measurement of what this engine actually gets wrong.",
    )
    args = parser.parse_args()

    if not args.manifest.is_file():
        raise SystemExit(f"no manifest at {args.manifest}; run `make videos` first")

    truth = truth_from_manifest(args.manifest)
    capable, scope = (None, None) if args.all_cameras else capable_cameras(args.api)
    if args.all_cameras:
        scope = ("ALL cameras, including those that cannot resolve a plate — "
                 "not a quotable figure")
    pairs: list[dict[str, str]] | None = [] if args.dump_pairs else None
    refusals: Counter[str] = Counter()
    render(
        score(args.api, args.minutes, truth, args.prefix, capable, pairs, refusals),
        truth, args.minutes, scope,
    )

    if pairs is not None and args.dump_pairs:
        args.dump_pairs.parent.mkdir(parents=True, exist_ok=True)
        args.dump_pairs.write_text(json.dumps(pairs, indent=2) + "\n")
        exact = sum(1 for p in pairs if p["read"] == p["truth"])
        corrupted = len(pairs) - exact
        print(f"\n{DIM}{len(pairs)} attributed reads written to {args.dump_pairs} "
              f"({exact} exact, {corrupted} corrupted).{RESET}")

        # Say why reads were turned away. Without this, "0 corrupted" is equally
        # consistent with attribution being broken and with it working exactly
        # as designed — and those call for opposite responses.
        refused = sum(refusals.values())
        if refused:
            print(f"{DIM}{refused} read(s) not attributed:{RESET}")
            for why, n in refusals.most_common():
                print(f"{DIM}    {n:>4}  {why}{RESET}")

        if corrupted < MIN_CORRUPTED_TO_LEARN:
            need = MIN_CORRUPTED_TO_LEARN - corrupted
            print(f"{DIM}Need {need} more corrupted read(s) before a learned table "
                  f"beats the glyph-similarity prior. This is a *good* problem — "
                  f"it means the pipeline is reading cleanly — but it does mean "
                  f"`make confusion` (the prior) stays the right table for now.{RESET}")
            print(f"{DIM}To gather more: run a longer window, and include night and "
                  f"glare clips, where the errors actually are.{RESET}")
        else:
            print(f"{DIM}Next: python -m scripts.learn_confusion pairs "
                  f"--input {args.dump_pairs}{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
