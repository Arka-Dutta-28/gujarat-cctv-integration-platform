"""A localhost review tool for the plate-training corpus.

Run it, open the page, label crops. It exists so that verifying the corpus is
something a second person can do in spare minutes, rather than something only
the person who wrote verify.csv can do.

Why this and not the contact sheets. scripts/review_crops.py renders forty crops
onto one PNG with the pipeline's read printed underneath, and a reviewer scans
for disagreements. That is genuinely faster for triage and it stays. But its
speed comes from anchoring, the answer printed under the picture, and anchoring
is precisely what must not happen to the crops that will become the evaluation
set.

This project has already paid for that mistake once. The confusion learner
scored the corrected plate against truth, so every error the correction table
had already fixed was invisible to it; fixing the comparison took the usable
mistake count from 64 to 282 on the same recordings. Show a reviewer GIIEAFOSE2
under a crop and they will accept a near miss, and then the model is measured
against labels its own output shaped.

So this tool has two modes and records which one produced each label.

    blind     The crop, and nothing else. The pipeline's read is revealed after
              the label is submitted. After is useful, since it is how pipeline
              bugs get noticed, and costs nothing because the label is already
              written.
    triage    The read is shown alongside. Fast, anchored, and honest about it:
              every label carries anchored=true and apply_labels can exclude
              them from anything that scores the model.

Three buttons, not a text box. Measured on the 141 crops verified by hand so
far:

    not a plate at all   94   67%   signage, hoardings, name boards
    unreadable            7    5%   a plate is there; no human can read it
    a usable label       40   28%

The most common correct answer is "not a plate", so it is one keypress. The
second is "unreadable", which is not a failure label but the human floor the
recogniser gets measured against, and the evidence behind "0 of 30 cameras reach
ANPR grade". Collapsing it into "not a plate" would overstate how much of the
estate is readable.

Two people, and what happens when they disagree. Every tenth crop served is one
already labelled by someone else. That is the only way to know what the labels
are worth: if two reviewers disagree on 15% of crops, no fine-tuning result
inside 15% means anything. Disagreements are not silently resolved. apply_labels
marks the crop disputed and leaves it unverified, because a corpus that quietly
picks a winner destroys the one measurement the double-labelling was for.

Storage. Labels append to labels.jsonl, one event per submission, fsynced, never
rewritten. The manifest is only touched by an explicit --apply. A click during a
crash therefore costs at most itself, and the event log keeps who labelled what
and when, which a CSV cannot.

Privacy. The corpus is registration numbers of real vehicles.
data/corpus/PURPOSE.md holds it under "not served by the API, not copied outside
this machine", so this is a separate server that binds loopback and refuses
anything else without an explicit flag. It is never mounted into the platform's
API.

Usage::

    python -m scripts.review_server                      # http://127.0.0.1:8642
    python -m scripts.review_server --apply              # fold labels into the manifest
    python -m scripts.review_server --agreement          # inter-annotator report
"""

from __future__ import annotations

import argparse
import csv
import http.server
import io
import json
import logging
import os
import pathlib
import sys
import threading
import urllib.parse
from datetime import UTC, datetime

from scripts.review_crops import _within_band, plate_likeness

log = logging.getLogger("review-server")

#: Every Nth crop served to a labeller is one somebody else has already done.
#: Deterministic rather than random so the rate is exact on short sessions — at
#: 10% random, a 40-crop session can easily contain zero re-checks and then the
#: agreement number does not exist at all.
RECHECK_EVERY = 10

#: Loopback only. See the Privacy note above.
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}

#: The lowest `plate_likeness` band the queue will hand a reviewer.
#:
#: Band 0 is "the recogniser read no digits at all", and an Indian plate always
#: carries four. Measured over the first 141 verifications: **81 band-0 crops
#: labelled, 0 of them a plate** — every one was signage, a hoarding, an
#: institution name board or `GSRTC` painted on a bus. Band 2 by comparison runs
#: about one plate in four.
#:
#: Band 0 was also 60% of the corpus, so serving it is why a reviewer's honest
#: impression is "most of these are not plates". With 81 samples and no
#: successes the rule of three puts the true rate below ~3.6%, i.e. at most a
#: handful of plates hiding in several hundred crops — roughly 29 crops reviewed
#: per plate found, against 4 in band 2.
#:
#: They stay *in* the corpus, because "the pipeline read the camera's own label
#: here" is a useful negative and deleting rows makes it unauditable. They are
#: simply not what a human's attention is spent on. `--min-band 0` serves them.
MIN_QUEUE_BAND = 1

VERDICTS = ("plate", "not_a_plate", "unreadable", "skip")


# --------------------------------------------------------------------------
# Corpus state. Pure functions over the manifest and the event log, so the
# queue and the apply semantics are testable without going near a socket.
# --------------------------------------------------------------------------


def load_manifest(corpus: pathlib.Path) -> list[dict]:
    lines = (corpus / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def load_events(corpus: pathlib.Path) -> list[dict]:
    """Every label ever submitted, oldest first. Missing file means none yet."""
    path = corpus / "labels.jsonl"
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # A torn final line from a kill mid-write. Skipping it loses one
                # label; refusing to start loses the session.
                log.warning("skipping unparseable event line")
    return events


def band_of(row: dict) -> int:
    """The crop's plate-likeness band. Named so callers read as intent."""
    return plate_likeness(row.get("read", ""), row.get("format_valid", False))


def queue_order(rows: list[dict]) -> list[dict]:
    """Most plate-like first, using the same ranking as the contact sheets.

    Sorting by crop width is the intuitive choice and it is backwards — a shop
    sign is physically wider in frame than a plate at distance, so width-first
    fills the front of the queue with furniture. `plate_likeness` is what fixed
    that for the sheets and there is no reason for the two tools to disagree
    about what is worth looking at first.
    """
    return sorted(
        rows,
        key=lambda r: (-plate_likeness(r.get("read", ""), r.get("format_valid", False)),
                       _within_band(r)),
    )


def latest_by_labeller(events: list[dict]) -> dict[str, dict[str, dict]]:
    """``{sighting_id: {labeller: event}}`` keeping only each labeller's last word.

    Last word rather than first: a reviewer who corrects their own typo means
    the correction, and the whole history is still in the log.
    """
    out: dict[str, dict[str, dict]] = {}
    for event in events:
        if event.get("verdict") == "skip":
            continue
        out.setdefault(event["sighting_id"], {})[event["labeller"]] = event
    return out


def verified_plates(corpus: pathlib.Path) -> list[dict]:
    """Manifest rows every labeller agreed are a plate, with the agreed text.

    This is the recogniser's test set:
    nothing may train on it or choose a checkpoint by it. A crop counts only if
    everyone who judged it said "plate" and typed the same characters.
    """
    rows = {r["sighting_id"]: r for r in load_manifest(corpus)}
    out = []
    for sid, labels in latest_by_labeller(load_events(corpus)).items():
        verdicts = {e["verdict"] for e in labels.values()}
        truths = {e["truth"] for e in labels.values() if e["verdict"] == "plate"}
        if verdicts == {"plate"} and len(truths) == 1:
            out.append({**rows[sid], "truth": truths.pop()})
    return sorted(out, key=lambda r: r["sighting_id"])


def choose_next(
    rows: list[dict],
    events: list[dict],
    labeller: str,
    served: int,
    *,
    recheck_every: int = RECHECK_EVERY,
    min_band: int = MIN_QUEUE_BAND,
    first_condition: str | None = None,
) -> tuple[dict | None, bool]:
    """The next crop for this labeller, and whether it is a re-check.

    ``served`` is how many crops this labeller has already been handed this
    session; it is what makes the re-check cadence exact rather than probable.

    ``min_band`` keeps crops the recogniser read no digits from being handed out
    — see `MIN_QUEUE_BAND` for the measurement. A crop already labelled by
    somebody else is exempt: a re-check exists to compare two people on the
    *same* image, and skipping it because of its band would quietly bias the
    agreement figure toward easy crops.

    ``first_condition`` (e.g. ``"night"``) moves that condition's crops to the
    front of new work, keeping plate-likeness order within each group. Added
    14 Sep 2026: 1 of the 41 verified plates was night, and night is what the
    recogniser work is meant to improve.
    """
    labelled = latest_by_labeller(events)
    mine = {sid for sid, by in labelled.items() if labeller in by}
    rows = [r for r in rows if band_of(r) >= min_band or r["sighting_id"] in labelled]

    if served and recheck_every and served % recheck_every == 0:
        # Somebody else's label, that I have not seen. Oldest first so the
        # backlog of un-agreed crops drains rather than resampling recent ones.
        others = [
            r for r in rows
            if r["sighting_id"] in labelled and r["sighting_id"] not in mine
        ]
        if others:
            return queue_order(others)[0], True

    # New work means *nobody* has labelled it, not merely "not me". Serving a
    # crop somebody else has done as though it were fresh would have handed a
    # reviewer all 141 contact-sheet verifications first — they rank highest,
    # being the most plate-like — and double-labelled a fifth of the corpus
    # before touching anything new. Re-checks are deliberate and 10%; they are
    # not the default path.
    ordered = queue_order(rows)
    if first_condition:
        ordered.sort(key=lambda r: r.get("condition") != first_condition)  # stable
    for row in ordered:
        if row["sighting_id"] not in labelled:
            return row, False

    # Nothing unlabelled left. Everything from here is a second opinion, and is
    # reported as one.
    for row in ordered:
        if row["sighting_id"] not in mine:
            return row, True
    return None, False


def label_event(
    row: dict,
    *,
    verdict: str,
    truth: str,
    note: str,
    labeller: str,
    mode: str,
    recheck: bool,
) -> dict:
    """One immutable record of a human looking at one crop."""
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    return {
        "sighting_id": row["sighting_id"],
        "image": row["image"],
        "verdict": verdict,
        # Upper-cased here so the marker comparison downstream is unambiguous.
        # `x` becoming `X` silently re-classified 81 pieces of furniture as
        # verified plates the first time the CSV path did this wrong.
        "truth": truth.strip().upper() if verdict == "plate" else None,
        "note": note.strip(),
        "labeller": labeller,
        "mode": mode,
        # The whole reason the modes are separate. A label taken with the
        # machine's read on screen is evidence of a different thing.
        "anchored": mode == "triage",
        "recheck": recheck,
        # What the pipeline said, frozen at label time — so an event stays
        # interpretable after the recogniser changes.
        "machine_read": row.get("read", ""),
        "ts": datetime.now(UTC).isoformat(),
    }


def _verdict_of(event: dict) -> tuple[str, str | None]:
    return event["verdict"], event.get("truth")


def agreement(events: list[dict]) -> dict:
    """Inter-annotator agreement over crops more than one person labelled.

    Reported as two numbers because they answer different questions: whether
    two people put a crop in the same *category*, and whether they read the same
    *characters* off it. The second is always the lower one and it is the one
    that bounds what a fine-tuning result can claim.
    """
    labelled = latest_by_labeller(events)
    both = {sid: by for sid, by in labelled.items() if len(by) > 1}
    category = exact = 0
    plates = 0
    disputes: list[str] = []
    for sid, by in both.items():
        verdicts = {e["verdict"] for e in by.values()}
        if len(verdicts) == 1:
            category += 1
            if verdicts == {"plate"}:
                plates += 1
                if len({e["truth"] for e in by.values()}) == 1:
                    exact += 1
                else:
                    disputes.append(sid)
        else:
            disputes.append(sid)
    return {
        "double_labelled": len(both),
        "category_agreement": category,
        "plate_pairs": plates,
        "exact_agreement": exact,
        "disputed": sorted(disputes),
    }


#: Whose labels the existing manifest verifications become. Named rather than
#: blank so the agreement report says where a disagreement came from.
SEED_LABELLER = "contact-sheets"


def seed_from_manifest(corpus: pathlib.Path) -> int:
    """Turn verifications already in the manifest into events, once.

    141 crops were verified through scripts/review_crops.py before this tool
    existed. Without this they have no events, so the queue would hand them straight
    back and a reviewer would spend a fifth of the corpus redoing finished work.

    Seeding them does something better than avoiding that. Those labels were taken
    off contact sheets with the pipeline's read printed underneath, anchored by this
    tool's own definition, so once they are events the every-tenth re-check starts
    silently measuring them against fresh blind labels. Whether the anchored pass
    was any good is a real open question, and this answers it for free.

    Idempotent: if this labeller already has events, nothing is written.
    """
    events = load_events(corpus)
    if any(e.get("labeller") == SEED_LABELLER for e in events):
        return 0
    written = 0
    with (corpus / "labels.jsonl").open("a", encoding="utf-8") as handle:
        for row in load_manifest(corpus):
            if not row.get("verified"):
                continue
            if row.get("not_a_plate"):
                verdict, truth = "not_a_plate", ""
            elif row.get("unreadable"):
                verdict, truth = "unreadable", ""
            elif row.get("truth"):
                verdict, truth = "plate", row["truth"]
            else:
                continue
            event = label_event(
                row, verdict=verdict, truth=truth, note=row.get("note", ""),
                labeller=SEED_LABELLER, mode="triage", recheck=False,
            )
            # The timestamp is when the label was *migrated*, not when it was
            # made, and pretending otherwise would put a fiction in the log.
            event["seeded"] = True
            handle.write(json.dumps(event) + "\n")
            written += 1
        handle.flush()
        os.fsync(handle.fileno())
    return written


def rename_labeller(corpus: pathlib.Path, old: str, new: str) -> int:
    """Reattribute one labeller's events to another, keeping a backup.

    The log is append-only everywhere else, and this is the one operation that
    cannot be expressed that way: identity is the key the agreement report
    groups by, so a wrong name is not a wrong *value* to be superseded by a
    later event — it is a second person who does not exist.

    That matters more than tidiness. A reviewer who labels under someone else's
    name becomes their own second annotator: every crop they later redo counts
    as double-labelled, and where they changed their mind it counts as a
    *disagreement*. Both numbers then describe one person's consistency while
    claiming to describe two people's agreement, which is the one measurement
    this tool exists to produce.

    Merging is last-word-wins, exactly as if the events had always carried the
    right name — `latest_by_labeller` keeps each labeller's final answer, so a
    correction made later under the right name supersedes the mistake.
    """
    path = corpus / "labels.jsonl"
    events = load_events(corpus)
    touched = [e for e in events if e.get("labeller") == old]
    if not touched:
        return 0
    (corpus / "labels.jsonl.bak").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )
    for event in touched:
        event["labeller"] = new
        # Kept so the reattribution is visible in the log rather than silent.
        event["renamed_from"] = old
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return len(touched)


def apply_labels(corpus: pathlib.Path, *, include_anchored: bool = True) -> dict:
    """Fold the event log into the manifest, refusing to resolve disagreements.

    Same field semantics as `scripts.review_crops.apply_verification` — the two
    tools write the same manifest and must not disagree about what a verified
    row looks like.
    """
    rows = load_manifest(corpus)
    events = load_events(corpus)
    if not include_anchored:
        events = [e for e in events if not e.get("anchored")]
    labelled = latest_by_labeller(events)

    stats = {"plates": 0, "not_a_plate": 0, "unreadable": 0, "disputed": 0}
    for row in rows:
        by = labelled.get(row["sighting_id"])
        if not by:
            continue
        decisions = {_verdict_of(e) for e in by.values()}
        if len(decisions) > 1:
            # Two people looked and did not agree. Picking one would delete the
            # only signal the double-labelling exists to produce, so the crop
            # stays unverified and says so.
            row["disputed"] = True
            row["verified"] = False
            stats["disputed"] += 1
            continue
        row.pop("disputed", None)
        verdict, truth = decisions.pop()
        row["verified"] = True
        row["truth"] = truth if verdict == "plate" else None
        row["not_a_plate"] = verdict == "not_a_plate"
        row["unreadable"] = verdict == "unreadable"
        note = next((e["note"] for e in by.values() if e.get("note")), "")
        if note:
            row["note"] = note
        stats["plates" if verdict == "plate" else verdict] += 1

    (corpus / "manifest.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    stats["unreviewed"] = sum(1 for r in rows if not r.get("verified"))
    return stats


def row_payload(row: dict, recheck: bool, mode: str) -> dict:
    """What the browser is told about a crop before the reviewer answers.

    The `if` below is the whole blind mode. It is a module-level function and
    directly tested for exactly that reason: everything else here is
    convenience, and this is the one line that decides whether the labels are
    independent of the recogniser or a correction of it.
    """
    payload = {
        "sighting_id": row["sighting_id"],
        "crop": f"/crop/{urllib.parse.quote(row['sighting_id'])}",
        "plate_px": row.get("plate_px"),
        "condition": row.get("condition"),
        "camera_name": row.get("camera_name"),
        "recheck": recheck,
    }
    if mode == "triage":
        payload["machine_read"] = row.get("read", "")
        payload["confidence"] = row.get("confidence")
    return payload


def row_status(by: dict[str, dict], labeller: str) -> dict:
    """What is known about one crop: my answer, everyone else's, and whether they clash."""
    mine = by.get(labeller)
    decisions = {(e["verdict"], e.get("truth")) for e in by.values()}
    return {
        "verdict": mine["verdict"] if mine else None,
        "truth": mine.get("truth") if mine else None,
        "anchored": bool(mine and mine.get("anchored")),
        "labellers": sorted(by),
        "others": [
            {"labeller": who, "verdict": e["verdict"], "truth": e.get("truth")}
            for who, e in sorted(by.items()) if who != labeller
        ],
        "disputed": len(decisions) > 1,
    }


def table_rows(
    rows: list[dict],
    events: list[dict],
    labeller: str,
    *,
    mode: str = "blind",
    reveal: bool = False,
) -> list[dict]:
    """Every crop and its labels, in queue order: the whole sheet at once.

    The machine's read is withheld on crops this labeller has not answered yet, for
    the same reason the review card withholds it. A table printing the recogniser's
    guess next to 521 unlabelled thumbnails is the anchoring problem with a
    scrollbar. It is not hidden as policy, since `reveal` turns it on for everything
    and the CSV export always carries it, but it is off by default so that seeing it
    is a decision rather than an accident.
    """
    labelled = latest_by_labeller(events)
    out = []
    for row in queue_order(rows):
        status = row_status(labelled.get(row["sighting_id"], {}), labeller)
        item = {
            "sighting_id": row["sighting_id"],
            "crop": f"/crop/{urllib.parse.quote(row['sighting_id'])}",
            "camera": row.get("camera"),
            "camera_name": row.get("camera_name"),
            "condition": row.get("condition"),
            "plate_px": row.get("plate_px"),
            "format_valid": row.get("format_valid", False),
            "confidence": row.get("confidence"),
            "ts": row.get("ts"),
            "band": band_of(row),
            **status,
        }
        if reveal or mode == "triage" or status["verdict"] is not None:
            item["machine_read"] = row.get("read", "")
        out.append(item)
    return out


#: Column order for the export. `machine_read` and `truth` sit next to each
#: other because comparing them is the only reason anyone opens this file.
EXPORT_COLUMNS = (
    "sighting_id", "camera", "camera_name", "condition", "plate_px",
    "confidence", "format_valid", "band", "machine_read", "verdict", "truth",
    "labellers", "others", "disputed", "anchored", "ts",
)


def export_csv(rows: list[dict], events: list[dict], labeller: str) -> str:
    """The whole corpus as one sheet, machine reads included.

    Always revealed here: an export is opened in a spreadsheet after the fact,
    not while labelling, so withholding the column would only make the file
    useless for the comparison it exists for.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for item in table_rows(rows, events, labeller, reveal=True):
        writer.writerow({
            **item,
            "labellers": " ".join(item["labellers"]),
            "others": " ".join(
                f"{o['labeller']}={o['truth'] or o['verdict']}" for o in item["others"]
            ),
        })
    return buffer.getvalue()


def progress(rows: list[dict], events: list[dict]) -> dict:
    labelled = latest_by_labeller(events)
    counts = {"plate": 0, "not_a_plate": 0, "unreadable": 0}
    for by in labelled.values():
        verdicts = {e["verdict"] for e in by.values()}
        if len(verdicts) == 1:
            counts[verdicts.pop()] += 1
    return {
        "total": len(rows),
        "labelled": len(labelled),
        "remaining": len(rows) - len(labelled),
        **counts,
        **agreement(events),
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Corpus:
    """Shared mutable state behind a lock, so two open tabs cannot tear the log."""

    def __init__(self, root: pathlib.Path, *, min_band: int = MIN_QUEUE_BAND,
                 first_condition: str | None = None) -> None:
        self.root = root
        self.min_band = min_band
        self.first_condition = first_condition
        self.rows = load_manifest(root)
        self.by_id = {r["sighting_id"]: r for r in self.rows}
        self.events = load_events(root)
        self.served: dict[str, int] = {}
        self._lock = threading.Lock()

    def next_for(self, labeller: str) -> tuple[dict | None, bool]:
        with self._lock:
            served = self.served.get(labeller, 0)
            row, recheck = choose_next(self.rows, self.events, labeller, served,
                                       min_band=self.min_band,
                                       first_condition=self.first_condition)
            if row is not None:
                self.served[labeller] = served + 1
            return row, recheck

    def record(self, event: dict) -> None:
        with self._lock:
            path = self.root / "labels.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")
                handle.flush()
                # A label is cheap to give and annoying to give twice. fsync so
                # a session survives the machine going down mid-review.
                os.fsync(handle.fileno())
            self.events.append(event)

    def others_verdict(self, sighting_id: str, labeller: str) -> dict | None:
        with self._lock:
            by = latest_by_labeller(self.events).get(sighting_id, {})
        for who, event in by.items():
            if who != labeller:
                return {"labeller": who, "verdict": event["verdict"], "truth": event["truth"]}
        return None

    def table(self, labeller: str, mode: str, reveal: bool) -> list[dict]:
        with self._lock:
            return table_rows(self.rows, list(self.events), labeller,
                              mode=mode, reveal=reveal)

    def one(self, sighting_id: str, labeller: str) -> tuple[dict | None, bool]:
        """A crop asked for by name — manual navigation, not the queue.

        `recheck` is recomputed rather than trusted from the client, so the
        second-opinion banner is honest however the reviewer arrived here.
        """
        row = self.by_id.get(sighting_id)
        if row is None:
            return None, False
        with self._lock:
            by = latest_by_labeller(self.events).get(sighting_id, {})
        return row, any(who != labeller for who in by)

    def csv(self, labeller: str) -> str:
        with self._lock:
            return export_csv(self.rows, list(self.events), labeller)

    def snapshot(self) -> dict:
        with self._lock:
            return progress(self.rows, list(self.events))


class Handler(http.server.BaseHTTPRequestHandler):
    corpus: Corpus  # set by serve()

    server_version = "corpus-review"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A002 - stdlib signature
        log.debug(fmt, *args)

    # -- helpers ---------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The corpus is personal data on a page that must not be cached or
        # embedded anywhere else, even locally.
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return

        if parsed.path == "/api/next":
            labeller = (query.get("labeller") or [""])[0].strip()
            mode = (query.get("mode") or ["blind"])[0]
            if not labeller:
                self._json({"error": "labeller required"}, 400)
                return
            row, recheck = self.corpus.next_for(labeller)
            if row is None:
                self._json({"done": True, "progress": self.corpus.snapshot()})
                return
            self._json({
                "item": row_payload(row, recheck, mode),
                "progress": self.corpus.snapshot(),
            })
            return

        if parsed.path == "/api/items":
            labeller = (query.get("labeller") or [""])[0].strip()
            if not labeller:
                self._json({"error": "labeller required"}, 400)
                return
            self._json({
                "items": self.corpus.table(
                    labeller,
                    (query.get("mode") or ["blind"])[0],
                    (query.get("reveal") or [""])[0] == "1",
                ),
                "progress": self.corpus.snapshot(),
            })
            return

        if parsed.path.startswith("/api/item/"):
            labeller = (query.get("labeller") or [""])[0].strip()
            mode = (query.get("mode") or ["blind"])[0]
            sighting_id = urllib.parse.unquote(parsed.path[len("/api/item/"):])
            row, recheck = self.corpus.one(sighting_id, labeller)
            if row is None or not labeller:
                self._json({"error": "unknown crop or labeller"}, 400)
                return
            self._json({
                "item": row_payload(row, recheck, mode),
                "progress": self.corpus.snapshot(),
            })
            return

        if parsed.path == "/api/export.csv":
            labeller = (query.get("labeller") or [""])[0].strip()
            if not labeller:
                self._send(400, b"labeller required", "text/plain")
                return
            body = self.corpus.csv(labeller).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="corpus-review.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/stats":
            self._json(self.corpus.snapshot())
            return

        if parsed.path.startswith("/crop/"):
            sighting_id = urllib.parse.unquote(parsed.path[len("/crop/"):])
            row = self.corpus.by_id.get(sighting_id)
            if row is None:
                self._send(404, b"no such crop", "text/plain")
                return
            # Resolved against the corpus root and checked, so a crafted
            # `image` field cannot walk out of the corpus directory.
            path = (self.corpus.root / row["image"]).resolve()
            if not path.is_file() or self.corpus.root.resolve() not in path.parents:
                self._send(404, b"missing image", "text/plain")
                return
            self._send(200, path.read_bytes(), "image/png")
            return

        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - stdlib signature
        if urllib.parse.urlparse(self.path).path != "/api/label":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json({"error": "bad json"}, 400)
            return

        row = self.corpus.by_id.get(str(body.get("sighting_id", "")))
        labeller = str(body.get("labeller", "")).strip()
        if row is None or not labeller:
            self._json({"error": "unknown crop or labeller"}, 400)
            return
        try:
            event = label_event(
                row,
                verdict=str(body.get("verdict", "")),
                truth=str(body.get("truth", "")),
                note=str(body.get("note", "")),
                labeller=labeller,
                mode=str(body.get("mode", "blind")),
                recheck=bool(body.get("recheck")),
            )
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
            return

        other = None
        if event["recheck"]:
            other = self.corpus.others_verdict(row["sighting_id"], labeller)
        self.corpus.record(event)
        # The reveal. Only now — the label is already durable, so nothing the
        # reviewer sees here can change what they submitted.
        self._json({
            "machine_read": row.get("read", ""),
            "format_valid": row.get("format_valid", False),
            "confidence": row.get("confidence"),
            "camera_name": row.get("camera_name"),
            "condition": row.get("condition"),
            "plate_px": row.get("plate_px"),
            "other": other,
            "progress": self.corpus.snapshot(),
        })


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Corpus review</title>
<style>
  :root { color-scheme: dark; --bg:#12141a; --card:#1b1e27; --line:#2b3040;
          --ink:#e8eaf2; --dim:#8b93a8; --ok:#5ad18a; --warn:#f0b45e; --bad:#f07070;
          --blue:#2f6feb; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; }
  header { display:flex; gap:12px; align-items:center; padding:10px 16px;
           border-bottom:1px solid var(--line); flex-wrap:wrap; position:sticky;
           top:0; background:var(--bg); z-index:5; }
  header b { font-size:15px; }
  .grow { flex:1; }
  .pill { background:var(--card); border:1px solid var(--line); border-radius:999px;
          padding:3px 10px; color:var(--dim); font-size:12px; white-space:nowrap; }
  main { max-width:920px; margin:0 auto; padding:20px 16px 60px; }
  main.wide { max-width:1400px; }
  .stage { background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:20px; text-align:center; }
  #crop { max-width:100%; background:#000; border-radius:6px; }
  .meta { color:var(--dim); font-size:12px; margin-top:10px; }
  .row { display:flex; gap:10px; justify-content:center; flex-wrap:wrap; margin-top:16px; }
  button { background:#242938; color:var(--ink); border:1px solid var(--line);
           border-radius:8px; padding:10px 16px; font:inherit; cursor:pointer; }
  button:hover { border-color:#3b4157; background:#2c3243; }
  button.primary { background:var(--blue); border-color:var(--blue); }
  button.small { padding:5px 10px; font-size:12px; }
  button[disabled] { opacity:.4; cursor:default; }
  button.on { background:var(--blue); border-color:var(--blue); }
  input[type=text] { background:#0e1016; color:var(--ink); border:1px solid var(--line);
                     border-radius:8px; padding:10px 12px; font:16px/1 ui-monospace,monospace;
                     letter-spacing:2px; text-transform:uppercase; width:240px; text-align:center; }
  .reveal { margin-top:16px; padding:12px; border-radius:8px; border:1px solid var(--line);
            background:#0e1016; display:none; text-align:left; }
  .reveal.show { display:block; }
  .reveal code, code { font-family:ui-monospace,monospace; }
  .agree { color:var(--ok); } .differ { color:var(--bad); }
  .keys { color:var(--dim); font-size:12px; margin-top:14px; }
  .keys kbd { background:#0e1016; border:1px solid var(--line); border-radius:4px;
              padding:1px 6px; font:12px ui-monospace,monospace; }
  .banner { background:#20242f; border:1px solid var(--line);
            border-left:3px solid var(--warn); border-radius:8px; padding:10px 14px;
            margin-bottom:16px; color:var(--dim); font-size:13px; }
  .recheck { border-left-color:var(--blue); color:#9fc0ff; }
  label.zoom { color:var(--dim); font-size:12px; }
  /* An inline panel rather than prompt(): a modal dialog blocks the page for
     anything driving the browser, and a name box is not worth a dialog. */
  .gate { position:fixed; inset:0; background:rgba(10,11,15,.9); display:none;
          align-items:center; justify-content:center; z-index:10; }
  .gate.show { display:flex; }
  .gate-card { background:var(--card); border:1px solid var(--line); border-radius:12px;
               padding:24px; max-width:420px; text-align:center; }
  .gate-card p { color:var(--dim); font-size:13px; }
  .gate-card input { display:block; margin:14px auto; text-align:center; }
  /* navigation */
  .nav { display:flex; gap:10px; align-items:center; justify-content:center;
         margin-bottom:14px; color:var(--dim); font-size:12px; }
  .nav input[type=number] { background:#0e1016; color:var(--ink); border:1px solid var(--line);
                            border-radius:6px; padding:4px 8px; width:80px; font:13px/1 inherit;
                            text-align:center; }
  /* table */
  .tools { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:12px; }
  .sheet { width:100%; border-collapse:collapse; font-size:13px; }
  .sheet th { text-align:left; color:var(--dim); font-weight:600; font-size:11px;
              text-transform:uppercase; letter-spacing:.04em; padding:8px 10px;
              border-bottom:1px solid var(--line); position:sticky; top:57px;
              background:var(--bg); }
  .sheet td { padding:6px 10px; border-bottom:1px solid #21242e; vertical-align:middle; }
  .sheet tr:hover td { background:#191c25; cursor:pointer; }
  .sheet img { height:30px; background:#000; border-radius:3px; display:block; }
  .sheet .mono { font-family:ui-monospace,monospace; letter-spacing:1px; }
  .tag { border-radius:4px; padding:1px 7px; font-size:11px; white-space:nowrap; }
  .t-plate { background:#16351f; color:var(--ok); }
  .t-not_a_plate { background:#2a2d38; color:var(--dim); }
  .t-unreadable { background:#3a2f16; color:var(--warn); }
  .t-todo { background:#1c2230; color:#7f8dab; }
  .t-disputed { background:#3a1c1c; color:var(--bad); }
  .hidden { display:none !important; }
  .note { color:var(--dim); font-size:12px; margin:10px 0 0; }
</style>

<div id="gate" class="gate">
  <div class="gate-card">
    <b>Who is reviewing?</b>
    <p>Recorded with every label, so two people's readings can be compared.
       Your own name or initials &mdash; nothing else is stored about you.</p>
    <input type="text" id="gate-name" placeholder="name or initials" autocomplete="off">
    <button class="primary" id="gate-go">Start reviewing</button>
  </div>
</div>

<header>
  <b>Corpus review</b>
  <span class="pill" id="who">&hellip;</span>
  <span class="pill" id="mode-pill">blind</span>
  <span class="grow"></span>
  <span class="pill" id="progress">&hellip;</span>
  <span class="pill" id="agreement">&hellip;</span>
  <label class="zoom">zoom <input id="zoom" type="range" min="1" max="8" value="4"></label>
  <button class="small" id="switch">triage mode</button>
  <button class="small" id="view">table</button>
</header>

<main id="main">
  <!-- ---------------- review ---------------- -->
  <div id="review-view">
    <div class="banner" id="banner"></div>
    <div class="nav">
      <button class="small" id="first">&laquo; first</button>
      <button class="small" id="prev">&larr; previous</button>
      <span id="position">&hellip;</span>
      <button class="small" id="next">next &rarr;</button>
      <button class="small" id="todo">next unlabelled</button>
      <input type="number" id="jump" min="1" placeholder="go to #">
    </div>
    <div class="stage">
      <img id="crop" alt="plate crop">
      <div class="meta" id="meta"></div>
      <div class="row">
        <input type="text" id="plate" placeholder="GJ01AB1234"
               autocomplete="off" spellcheck="false">
        <button class="primary" id="submit-plate">Save plate</button>
      </div>
      <div class="row">
        <button id="not-plate">Not a plate</button>
        <button id="unreadable">Can't read it</button>
        <button id="skip">Skip</button>
      </div>
      <div class="keys">
        <kbd>1</kbd> not a plate &middot; <kbd>2</kbd> can't read it &middot;
        <kbd>3</kbd> skip &middot; type a plate then <kbd>Enter</kbd> &middot;
        <kbd>&larr;</kbd> <kbd>&rarr;</kbd> move &middot;
        <kbd>Space</kbd> next after the reveal
      </div>
      <div class="reveal" id="reveal"></div>
    </div>
  </div>

  <!-- ---------------- table ---------------- -->
  <div id="table-view" class="hidden">
    <div class="tools">
      <button class="small on" data-filter="all">All</button>
      <button class="small" data-filter="todo">To do</button>
      <button class="small" data-filter="plate">Plates</button>
      <button class="small" data-filter="not_a_plate">Not a plate</button>
      <button class="small" data-filter="unreadable">Unreadable</button>
      <button class="small" data-filter="disputed">Disputed</button>
      <span class="grow"></span>
      <button class="small" id="reveal-all">show unlabelled reads</button>
      <button class="small" id="download">download CSV</button>
    </div>
    <p class="note" id="table-note"></p>
    <table class="sheet">
      <thead><tr>
        <th>#</th><th>crop</th><th>camera</th><th>cond</th><th>px</th>
        <th>software read</th><th>your label</th><th>others</th><th>status</th>
      </tr></thead>
      <tbody id="sheet-body"></tbody>
    </table>
  </div>
</main>

<script>
const $ = (id) => document.getElementById(id);
let labeller = (localStorage.getItem('corpus.labeller') || '').trim();
let mode = localStorage.getItem('corpus.mode') || 'blind';
let view = 'review';
let filter = 'all';
let revealAll = false;
let items = [];          // the whole corpus, in queue order
let idx = 0;             // where we are in it
let item = null;         // the crop currently on the card
let awaiting = false;

/* ---------------- sign in ---------------- */

function signIn(name) {
  labeller = name.trim();
  if (!labeller) { $('gate-name').focus(); return; }
  localStorage.setItem('corpus.labeller', labeller);
  $('who').textContent = labeller;
  $('gate').className = 'gate';
  boot();
}
$('gate-go').onclick = () => signIn($('gate-name').value);
$('gate-name').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); signIn($('gate-name').value); }
});

/* ---------------- chrome ---------------- */

function paintMode() {
  $('mode-pill').textContent = mode;
  $('switch').textContent = mode === 'blind' ? 'triage mode' : 'blind mode';
  $('banner').innerHTML = mode === 'blind'
    ? "You are looking at a crop and nothing else. Read the plate yourself. " +
      "What the software read is shown <b>after</b> you answer &mdash; so your label is yours, " +
      "not a correction of its guess."
    : "<b>Triage mode.</b> The software's read is shown with the crop. " +
      "Fast, but anchored &mdash; " +
      "these labels are marked <code>anchored</code> and are excluded from " +
      "anything that scores the model. Use it for &ldquo;is this a plate at all&rdquo;, " +
      "not for producing evaluation labels.";
  $('banner').className = 'banner';
}

$('switch').onclick = () => {
  mode = mode === 'blind' ? 'triage' : 'blind';
  localStorage.setItem('corpus.mode', mode);
  paintMode();
  refresh().then(() => view === 'review' ? showAt(idx) : paintTable());
};

$('view').onclick = () => {
  view = view === 'review' ? 'table' : 'review';
  $('view').textContent = view === 'review' ? 'table' : 'review';
  $('review-view').className = view === 'review' ? '' : 'hidden';
  $('table-view').className = view === 'table' ? '' : 'hidden';
  $('main').className = view === 'table' ? 'wide' : '';
  if (view === 'table') { refresh().then(paintTable); } else { showAt(idx); }
};

$('zoom').value = localStorage.getItem('corpus.zoom') || 4;
$('zoom').oninput = () => {
  localStorage.setItem('corpus.zoom', $('zoom').value);
  sizeCrop();
};

function sizeCrop() {
  if (!item || !item.plate_px) return;
  $('crop').style.width = Math.min(1100, item.plate_px * Number($('zoom').value)) + 'px';
}

function paintProgress(p) {
  $('progress').textContent = p.labelled + '/' + p.total + ' \\u00b7 ' + p.remaining + ' left';
  if (p.double_labelled) {
    const pct = Math.round(100 * p.exact_agreement / Math.max(1, p.plate_pairs));
    $('agreement').textContent = 'agreement ' + p.category_agreement + '/' + p.double_labelled +
      (p.plate_pairs ? ' \\u00b7 plates ' + pct + '%' : '');
  } else {
    $('agreement').textContent = 'agreement \\u2014';
  }
}

/* ---------------- data ---------------- */

/* Refreshes are serialised. Several things ask for one at once — boot, the
   view toggle, every label — and when two overlapped the table could paint
   from a half-replaced `items` and render a single row while the counter said
   662. One chain means a caller always awaits a settled list. */
let refreshing = Promise.resolve();

function refresh() {
  refreshing = refreshing.then(async () => {
    if (!labeller) return;
    const q = '?labeller=' + encodeURIComponent(labeller) + '&mode=' + mode +
              (revealAll ? '&reveal=1' : '');
    const data = await (await fetch('/api/items' + q)).json();
    items = data.items;
    paintProgress(data.progress);
  }).catch(() => {});
  return refreshing;
}

/* ---------------- review card ---------------- */

function positionText() {
  $('position').textContent = items.length ? (idx + 1) + ' of ' + items.length : '\\u2014';
  $('prev').disabled = idx <= 0;
  $('next').disabled = idx >= items.length - 1;
  $('first').disabled = idx <= 0;
}

async function showAt(n) {
  if (!labeller || !items.length) return;
  idx = Math.max(0, Math.min(items.length - 1, n));
  awaiting = false;
  $('reveal').className = 'reveal';
  $('plate').value = '';
  positionText();
  const id = items[idx].sighting_id;
  const q = '?labeller=' + encodeURIComponent(labeller) + '&mode=' + mode;
  const data = await (await fetch('/api/item/' + encodeURIComponent(id) + q)).json();
  paintProgress(data.progress);
  item = data.item;
  $('crop').src = item.crop;
  sizeCrop();
  const bits = [item.plate_px + 'px', item.condition, item.camera_name];
  if (mode === 'triage' && item.machine_read !== undefined) {
    bits.push('software read: ' + (item.machine_read || '(nothing)'));
  }
  const already = items[idx];
  if (already.verdict) {
    bits.push('you answered: ' + (already.truth || already.verdict));
  }
  $('meta').textContent = bits.filter(Boolean).join(' \\u00b7 ');
  if (item.recheck) {
    $('banner').className = 'banner recheck';
    $('banner').innerHTML = '<b>Second opinion.</b> Someone else has already labelled ' +
      'this one. Their answer is hidden until you give yours &mdash; that comparison is how we ' +
      'find out what the labels are worth.';
  } else {
    paintMode();
  }
  $('plate').focus();
}

/* Auto-advance uses the server's queue, so the every-tenth re-check cadence
   stays server-side and is the same whether or not anyone has been browsing. */
async function advance() {
  await refresh();
  const q = '?labeller=' + encodeURIComponent(labeller) + '&mode=' + mode;
  const data = await (await fetch('/api/next' + q)).json();
  paintProgress(data.progress);
  if (data.done) {
    $('meta').textContent = 'Everything has been labelled. Run make review-apply to fold it in.';
    $('reveal').className = 'reveal';
    return;
  }
  const at = items.findIndex((r) => r.sighting_id === data.item.sighting_id);
  showAt(at < 0 ? idx : at);
}

$('prev').onclick = () => showAt(idx - 1);
$('next').onclick = () => showAt(idx + 1);
$('first').onclick = () => showAt(0);
$('todo').onclick = () => advance();
$('jump').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const n = Number($('jump').value);
  if (n >= 1 && n <= items.length) showAt(n - 1);
  $('jump').value = '';
});

/* ---------------- labelling ---------------- */

async function send(verdict, truth) {
  if (!item || awaiting === true) return;
  awaiting = true;
  const r = await fetch('/api/label', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({sighting_id: item.sighting_id, labeller, mode,
                          recheck: item.recheck, verdict, truth: truth || ''}),
  });
  const data = await r.json();
  paintProgress(data.progress);
  if (verdict === 'skip') { advance(); return; }
  let h = '<div>software read <code>' + (data.machine_read || '(nothing)') + '</code>' +
          (data.format_valid ? ' \\u00b7 format valid' : '') +
          (data.confidence != null ? ' \\u00b7 confidence ' + data.confidence : '') + '</div>';
  if (data.other) {
    const same = data.other.verdict === verdict &&
                 (verdict !== 'plate' || data.other.truth === (truth || '').toUpperCase());
    h += '<div class="' + (same ? 'agree' : 'differ') + '">' +
         (same ? '\\u2713 agrees with ' : '\\u2717 differs from ') + data.other.labeller + ': ' +
         '<code>' + (data.other.truth || data.other.verdict) + '</code></div>';
  }
  h += '<div style="color:var(--dim);margin-top:6px">Space for the next one, ' +
       'or the arrows to look around.</div>';
  $('reveal').innerHTML = h;
  $('reveal').className = 'reveal show';
  awaiting = 'revealed';
  // Keep the sheet honest without leaving the card: a label taken here changes
  // a row over there, and a stale table is how a reviewer double-does work.
  refresh().then(() => { if (view === 'table') paintTable(); });
}

$('submit-plate').onclick = () => {
  const v = $('plate').value.trim();
  if (v) send('plate', v);
};
$('not-plate').onclick = () => send('not_a_plate');
$('unreadable').onclick = () => send('unreadable');
$('skip').onclick = () => send('skip');

document.addEventListener('keydown', (e) => {
  if (view !== 'review' || $('gate').classList.contains('show')) return;
  if (e.target === $('jump')) return;
  if (e.key === 'ArrowLeft') { e.preventDefault(); showAt(idx - 1); return; }
  if (e.key === 'ArrowRight') { e.preventDefault(); showAt(idx + 1); return; }
  if (awaiting === 'revealed') {
    if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); advance(); }
    return;
  }
  if (document.activeElement === $('plate') && $('plate').value) {
    if (e.key === 'Enter') { e.preventDefault(); $('submit-plate').click(); }
    return;
  }
  if (e.key === '1') { e.preventDefault(); $('not-plate').click(); }
  if (e.key === '2') { e.preventDefault(); $('unreadable').click(); }
  if (e.key === '3') { e.preventDefault(); $('skip').click(); }
  if (e.key === 'Enter' && $('plate').value.trim()) {
    e.preventDefault(); $('submit-plate').click();
  }
});

/* ---------------- the sheet ---------------- */

document.querySelectorAll('[data-filter]').forEach((b) => {
  b.onclick = () => {
    filter = b.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach(
      (o) => o.className = 'small' + (o === b ? ' on' : ''));
    paintTable();
  };
});

$('reveal-all').onclick = async () => {
  revealAll = !revealAll;
  $('reveal-all').className = 'small' + (revealAll ? ' on' : '');
  $('reveal-all').textContent = revealAll ? 'hide unlabelled reads' : 'show unlabelled reads';
  await refresh();
  paintTable();
};

$('download').onclick = () => {
  window.location = '/api/export.csv?labeller=' + encodeURIComponent(labeller);
};

function matches(r) {
  if (filter === 'all') return true;
  if (filter === 'todo') return !r.verdict;
  if (filter === 'disputed') return r.disputed;
  return r.verdict === filter;
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g,
    (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function paintTable() {
  const shown = items.map((r, i) => [r, i]).filter(([r]) => matches(r));
  $('table-note').innerHTML = shown.length + ' of ' + items.length + ' crops. ' +
    'Click any row to open it. ' + (revealAll
      ? '<b>Unlabelled reads are showing</b> &mdash; reading them before you label anchors ' +
        'your answer to the software\\u2019s guess.'
      : 'The software\\u2019s read is hidden on crops you have not answered yet, so seeing ' +
        'it stays a deliberate choice. The CSV export always includes it.');
  const body = $('sheet-body');
  body.innerHTML = '';
  const frag = document.createDocumentFragment();
  for (const [r, i] of shown) {
    const tr = document.createElement('tr');
    const status = r.disputed ? 'disputed' : (r.verdict || 'todo');
    const label = r.verdict === 'plate' ? r.truth : (r.verdict || '');
    tr.innerHTML =
      '<td style="color:var(--dim)">' + (i + 1) + '</td>' +
      '<td><img loading="lazy" src="' + r.crop + '" alt=""></td>' +
      '<td>' + escapeHtml(r.camera_name || r.camera || '') + '</td>' +
      '<td style="color:var(--dim)">' + escapeHtml(r.condition || '') + '</td>' +
      '<td style="color:var(--dim)">' + (r.plate_px == null ? '' : r.plate_px) + '</td>' +
      '<td class="mono">' + (r.machine_read === undefined
          ? '<span style="color:#525a6e">hidden</span>'
          : escapeHtml(r.machine_read || '(nothing)')) + '</td>' +
      '<td class="mono">' + escapeHtml(label) + '</td>' +
      '<td class="mono" style="color:var(--dim)">' +
        escapeHtml(r.others.map((o) => o.labeller + '=' + (o.truth || o.verdict)).join(' ')) +
      '</td>' +
      '<td><span class="tag t-' + status + '">' + status.replace(/_/g, ' ') + '</span>' +
        (r.anchored ? ' <span class="tag t-todo">anchored</span>' : '') + '</td>';
    tr.onclick = () => {
      view = 'review';
      $('view').textContent = 'table';
      $('review-view').className = '';
      $('table-view').className = 'hidden';
      $('main').className = '';
      showAt(i);
    };
    frag.appendChild(tr);
  }
  body.appendChild(frag);
}

/* ---------------- boot ---------------- */

async function boot() {
  paintMode();
  await refresh();
  /* Start where the server's queue says, not at row 1 of the sheet. For a
     labeller who has not answered anything, every row is "unlabelled by me" —
     so picking the first would open the contact-sheets set, which ranks
     highest, and start them re-doing finished work. The server prefers crops
     nobody has answered and keeps re-checks at one in ten; the page must not
     quietly disagree with it. */
  await advance();
}

if (labeller) {
  $('who').textContent = labeller;
  boot();
} else {
  $('gate').className = 'gate show';
  $('gate-name').focus();
}
</script>
"""


def serve(corpus: pathlib.Path, host: str, port: int, min_band: int,
          first_condition: str | None = None) -> int:
    state = Corpus(corpus, min_band=min_band, first_condition=first_condition)
    handler = type("BoundHandler", (Handler,), {"corpus": state})
    with http.server.ThreadingHTTPServer((host, port), handler) as httpd:
        bound = httpd.server_address
        snapshot = state.snapshot()
        withheld = sum(1 for r in state.rows if band_of(r) < min_band)
        log.info("corpus %s — %d crops, %d labelled, %d to go",
                 corpus, snapshot["total"], snapshot["labelled"], snapshot["remaining"])
        if withheld:
            log.info("%d crops below band %d are in the corpus but not in the "
                     "queue (0 of 81 such crops were ever a plate); --min-band 0 "
                     "serves them", withheld, min_band)
        if first_condition:
            log.info("new work starts with %s crops", first_condition)
        log.info("open http://%s:%d", host, bound[1])
        log.info("labels append to %s — nothing touches the manifest until --apply",
                 corpus / "labels.jsonl")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log.info("stopped. %d events recorded", len(state.events))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--corpus", type=pathlib.Path, default=pathlib.Path("data/corpus"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8642)
    parser.add_argument("--allow-remote", action="store_true",
                        help="bind a non-loopback address (the corpus is personal data)")
    parser.add_argument("--apply", action="store_true",
                        help="fold labels.jsonl into the manifest and exit")
    parser.add_argument("--exclude-anchored", action="store_true",
                        help="with --apply, ignore labels taken in triage mode")
    parser.add_argument("--agreement", action="store_true",
                        help="print the inter-annotator report and exit")
    parser.add_argument("--min-band", type=int, default=MIN_QUEUE_BAND,
                        help="lowest plate-likeness band to serve; 0 includes "
                             "reads with no digits, which measured 0 plates in 81")
    parser.add_argument("--first-condition", choices=["day", "night", "glare"],
                        help="serve this condition's unlabelled crops first")
    parser.add_argument("--no-seed", action="store_true",
                        help="do not migrate existing manifest verifications into the log")
    parser.add_argument("--rename-labeller", nargs=2, metavar=("OLD", "NEW"),
                        help="reattribute one labeller's events to another and exit")
    args = parser.parse_args(argv)

    if not (args.corpus / "manifest.jsonl").is_file():
        log.error("no manifest at %s — run scripts.harvest_plate_crops first", args.corpus)
        return 1

    if args.rename_labeller:
        old, new = args.rename_labeller
        moved = rename_labeller(args.corpus, old, new)
        log.info("%d events moved from %r to %r (backup at labels.jsonl.bak)",
                 moved, old, new)
        if moved:
            report = agreement(load_events(args.corpus))
            log.info("agreement now over %d double-labelled crops",
                     report["double_labelled"])
        return 0

    if args.agreement:
        report = agreement(load_events(args.corpus))
        log.info("%d crops labelled by more than one person", report["double_labelled"])
        log.info("  same category: %d", report["category_agreement"])
        log.info("  both read a plate: %d, identical characters: %d",
                 report["plate_pairs"], report["exact_agreement"])
        if report["disputed"]:
            log.info("  disputed: %s", ", ".join(report["disputed"]))
            log.info("A fine-tuning result inside this margin does not mean anything.")
        return 0

    if args.apply:
        stats = apply_labels(args.corpus, include_anchored=not args.exclude_anchored)
        log.info("%d plates, %d not a plate, %d unreadable, %d disputed, %d unreviewed",
                 stats["plates"], stats["not_a_plate"], stats["unreadable"],
                 stats["disputed"], stats["unreviewed"])
        if stats["disputed"]:
            log.info("Disputed crops are left unverified on purpose — resolving them "
                     "silently would delete the only measurement of label quality.")
        return 0

    if args.host not in _LOOPBACK and not args.allow_remote:
        log.error("refusing to bind %s: the corpus is real registration numbers and "
                  "PURPOSE.md holds it to this machine. Pass --allow-remote if you "
                  "genuinely mean it.", args.host)
        return 2

    if not args.no_seed:
        seeded = seed_from_manifest(args.corpus)
        if seeded:
            log.info("migrated %d existing verifications into the log as %r",
                     seeded, SEED_LABELLER)

    return serve(args.corpus, args.host, args.port, args.min_band, args.first_condition)


if __name__ == "__main__":
    sys.exit(main())
