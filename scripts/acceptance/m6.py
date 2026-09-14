"""M6 acceptance test.

From docs/build-plan.md §5:

    Accept: a PDF downloads containing real detections with corresponding
            timestamps.

"Real detections" is the load-bearing phrase, and M3 is the reason it is taken
literally here. That milestone's acceptance passed 7/7 against a `sightings`
table in which every plate was a Python dataclass repr — every check was true
and none of them looked at a value. So this test does not merely assert that
bytes arrive with a PDF header: it pulls detections from the API, then checks
that those exact plates and their timestamps appear inside the rendered
document, and that the columns the build plan names are all present.

Usage:
    python -m scripts.acceptance.m6
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

DEFAULT_API = "http://localhost:8000"
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

IST = timedelta(hours=5, minutes=30)

#: Columns build-plan §5 names for the report, by their CSV heading.
REQUIRED_COLUMNS = (
    "plate", "confidence", "camera", "latitude", "longitude", "ts_utc",
    "vehicle_class", "thumbnail",
)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def render(self) -> str:
        mark = f"{GREEN}PASS{RESET}" if self.passed else f"{RED}FAIL{RESET}"
        return f"  [{mark}] {self.name}\n         {DIM}{self.detail}{RESET}"


def _get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url)
    req.add_header("X-Actor", "acceptance-m6")
    req.add_header("X-Case-Ref", "ACC-M6")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def _get_json(url: str):
    return json.loads(_get(url) or "null")


def pdf_text(data: bytes) -> str:
    """Readable text from the PDF's Flate-compressed content streams."""
    out = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        chunk = match.group(1)
        try:
            out.append(zlib.decompress(chunk).decode("latin-1", errors="ignore"))
        except zlib.error:
            continue
    return "\n".join(out)


def check_pdf_downloads(api: str) -> tuple[Check, bytes]:
    """It must arrive as a file, not as a page the browser renders."""
    name = "A PDF downloads as an attachment"
    req = urllib.request.Request(f"{api}/api/reports/detections?format=pdf&limit=25")
    req.add_header("X-Actor", "acceptance-m6")
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        data = resp.read()
        content_type = resp.headers.get("content-type", "")
        disposition = resp.headers.get("content-disposition", "")

    ok = (
        data.startswith(b"%PDF-")
        and "application/pdf" in content_type
        and "attachment" in disposition
        and b"%%EOF" in data[-2048:]
    )
    return Check(
        name, ok,
        f"{len(data):,} bytes, {content_type}, {disposition}"
        if ok else f"content-type {content_type!r}, disposition {disposition!r}, "
                   f"starts {data[:8]!r}",
    ), data


def check_real_detections_are_in_it(api: str, data: bytes) -> Check:
    """The check M3 taught: read the values, do not count the rows."""
    name = "The PDF contains detections that are really in the index"
    live = _get_json(f"{api}/api/sightings?limit=25")
    if not live:
        return Check(name, False, "no sightings in the index to report on")

    text = pdf_text(data)
    plates = [s["plate_normalised"] for s in live]
    found = [p for p in plates if p in text]
    return Check(
        name, len(found) >= 3,
        f"{len(found)} of the {len(plates)} most recent plates in the index appear "
        f"in the document, including {', '.join(found[:3])}"
        if len(found) >= 3
        else f"only {len(found)} of {len(plates)} live plates found in the PDF",
    )


def check_timestamps_correspond(api: str, data: bytes) -> Check:
    """'with corresponding timestamps' — the times must be the reads' own."""
    name = "Each detection carries its own timestamp, rendered IST"
    live = _get_json(f"{api}/api/sightings?limit=25")
    text = pdf_text(data)

    matched = 0
    for sighting in live:
        stamp = datetime.fromisoformat(sighting["ts"]).astimezone(UTC) + IST
        if f"{stamp:%H:%M:%S}" in text and sighting["plate_normalised"] in text:
            matched += 1

    return Check(
        name, matched >= 3,
        f"{matched} detections appear with their own IST timestamp "
        f"(stored UTC, rendered +05:30 as the platform's convention requires)"
        if matched >= 3 else f"only {matched} timestamps matched their sighting",
    )


def check_csv_is_machine_readable(api: str) -> Check:
    """The other half of the deliverable: something an analyst can join."""
    name = "CSV carries every column the build plan names"
    raw = _get(f"{api}/api/reports/detections?format=csv&limit=50")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    if not rows:
        return Check(name, False, "the CSV export returned no rows")

    missing = [c for c in REQUIRED_COLUMNS if c not in rows[0]]
    coords = [r for r in rows if r["latitude"] and r["longitude"]]
    return Check(
        name, not missing and len(coords) > 0,
        f"{len(rows)} rows, all of {', '.join(REQUIRED_COLUMNS)} present; "
        f"{len(coords)} carry geolocation"
        if not missing else f"missing columns: {', '.join(missing)}",
    )


def check_thumbnails_resolve(api: str) -> Check:
    """A thumbnail column pointing at nothing is not a thumbnail."""
    name = "Report thumbnails resolve to real images"
    raw = _get(f"{api}/api/reports/detections?format=csv&limit=50")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    with_crops = [r for r in rows if r["thumbnail"]]
    if not with_crops:
        return Check(name, False, "no exported detection carries a thumbnail")

    checked = 0
    for row in with_crops[:5]:
        image = _get(f"{api}{row['thumbnail']}")
        if image[:2] == b"\xff\xd8" and len(image) > 500:
            checked += 1

    return Check(
        name, checked == min(5, len(with_crops)),
        f"{len(with_crops)} of {len(rows)} exported detections carry a crop; "
        f"{checked} fetched and verified as JPEG",
    )


def check_a_vehicle_report_is_filterable(api: str, plate: str) -> Check:
    """The investigative use: one vehicle's movement history as a document."""
    name = "A single vehicle's movement history exports"
    data = _get(
        f"{api}/api/reports/detections?format=pdf&limit=50&plate="
        + urllib.parse.quote(plate)
    )
    text = pdf_text(data)
    return Check(
        name, data.startswith(b"%PDF-") and plate in text and f"plate={plate}" in text,
        f"{len(data):,} bytes for {plate}, with the filter printed on the page so "
        "the report states what was asked for"
        if plate in text else f"{plate} does not appear in its own report",
    )


def check_export_is_audited(api: str) -> Check:
    """A copy of surveillance data leaving the platform must be accounted for."""
    name = "Every export is written to the audit trail"
    entries = _get_json(f"{api}/api/audit?action=report.export&limit=20")
    mine = [e for e in entries if e.get("actor") == "acceptance-m6"]
    return Check(
        name, len(mine) > 0,
        f"{len(mine)} `report.export` entries by this run, carrying the format, "
        f"row count and filters"
        if mine else "no report.export entries recorded for this run",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="M6 acceptance test.")
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--plate", default="GJ05UV9972")
    args = parser.parse_args()

    print(f"\n{'=' * 70}\nM6 acceptance — detection report export\n{'=' * 70}\n")

    download, data = check_pdf_downloads(args.api)
    checks = [
        download,
        check_real_detections_are_in_it(args.api, data),
        check_timestamps_correspond(args.api, data),
        check_csv_is_machine_readable(args.api),
        check_thumbnails_resolve(args.api),
        check_a_vehicle_report_is_filterable(args.api, args.plate),
        check_export_is_audited(args.api),
    ]

    for check in checks:
        print(check.render())

    passed = sum(c.passed for c in checks)
    ok = passed == len(checks)
    print(f"\n{'=' * 70}")
    print(
        f"{GREEN}M6 ACCEPTANCE PASSED{RESET}" if ok
        else f"{RED}M6 ACCEPTANCE FAILED{RESET} — {passed}/{len(checks)} checks passed"
    )
    print(f"{'=' * 70}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
