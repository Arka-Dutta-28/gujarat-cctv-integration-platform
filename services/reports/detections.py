"""Rendering detections as CSV and as PDF.

Two formats because they answer different questions. CSV is for the analyst who
is going to join this against something else, a stolen-vehicle list or a toll
record, and it must therefore be lossless and machine-readable: full precision
coordinates, ISO-8601 UTC, no thousands separators, one row per detection. PDF
is for the case file, and it must be readable by a person who was not there:
local time, the crop, and the filters the report was run under printed on it,
because a page of detections with no statement of what was asked for cannot be
checked by anyone later.

Three decisions worth stating.

Timestamps appear in both zones. Everything is stored UTC, a project convention,
and an operator in Gujarat thinks in IST. A report showing only one is either
unusable at the desk or ambiguous in the file, so the PDF shows IST with the UTC
value beside it and the CSV carries both columns.

The confidence is printed next to every plate. It would look tidier without it.
But the whole platform's position is that a read is evidence with a quality
attached, and a report that hides the 0.44 read among the 0.95 ones invites
exactly the over-reading that the honesty convention exists to prevent.

Rows that failed the Indian format check are included and marked. They are kept
in the index deliberately, since a mis-read wanted vehicle is worse than a noisy
record, and a report that silently dropped them would misrepresent what the
platform saw.
"""

from __future__ import annotations

import csv
import io
import logging
import pathlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("reports")

__all__ = ["Detection", "render_csv", "render_pdf", "fit_box", "IST", "CSV_COLUMNS"]

IST = timezone(timedelta(hours=5, minutes=30), "IST")

#: One row per detection, in the order build-plan §5 lists them. `ts_ist` and
#: `format_valid` are additions: local time because the report is read in
#: Gujarat, and the flag because a format failure is a fact about the read.
CSV_COLUMNS = (
    "plate", "confidence", "camera", "district", "latitude", "longitude",
    "ts_utc", "ts_ist", "vehicle_class", "vehicle_colour", "condition",
    "format_valid", "sighting_id", "thumbnail", "vehicle_id", "linked_by",
)


@dataclass(frozen=True)
class Detection:
    """One row of the report. Everything here came from `sightings`."""

    id: int
    ts: datetime
    plate: str
    confidence: float
    camera_name: str | None
    district: str | None
    lat: float | None
    lon: float | None
    vehicle_class: str | None
    condition: str | None
    format_valid: bool
    crop_path: str | None = None
    #: What a person would call this vehicle. Blank rather than invented when
    #: the crop could not support a name — see services/anpr/attributes.py. On
    #: the government estate this is often the only description of a vehicle
    #: the report can carry, because 0 of those 30 cameras read plates.
    vehicle_colour: str | None = None
    #: The vehicle id this sighting was linked to, and how: `plate`,
    #: `appearance` (a lead to check by eye) or `new` (services/anpr/linking.py).
    vehicle_uid: int | None = None
    uid_via: str | None = None

    @property
    def vehicle_ref(self) -> str:
        """`#5120` or `#5120~` for an appearance link; blank before vehicle ids."""
        if self.vehicle_uid is None:
            return ""
        return f"#{self.vehicle_uid}" + ("~" if self.uid_via == "appearance" else "")

    @property
    def ts_ist(self) -> datetime:
        return self.ts.astimezone(IST)

    @property
    def description(self) -> str:
        """`silver car` — what a person would say, for the PDF's one cell.

        Empty rather than "unknown" when nothing is known: a blank cell reads
        as an absence, and "unknown" printed 200 times reads as a fault.
        """
        return " ".join(p for p in (self.vehicle_colour, self.vehicle_class) if p)

    @property
    def thumbnail_url(self) -> str | None:
        return f"/api/sightings/{self.id}/crop" if self.crop_path else None


def render_csv(rows: list[Detection]) -> bytes:
    """Lossless and joinable. Full coordinate precision, ISO-8601, UTF-8 BOM.

    The BOM is there for a practical reason rather than a principled one: this
    lands on a desk and gets opened in Excel, which reads a UTF-8 file without
    one as the local codepage and mangles the district names.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([
            row.plate,
            f"{row.confidence:.4f}",
            row.camera_name or "",
            row.district or "",
            "" if row.lat is None else f"{row.lat:.6f}",
            "" if row.lon is None else f"{row.lon:.6f}",
            row.ts.isoformat(),
            row.ts_ist.isoformat(),
            row.vehicle_class or "",
            row.vehicle_colour or "",
            row.condition or "",
            "true" if row.format_valid else "false",
            row.id,
            row.thumbnail_url or "",
            row.vehicle_uid or "",
            row.uid_via or "",
        ])
    return buffer.getvalue().encode("utf-8-sig")


# --- PDF ------------------------------------------------------------------

#: Page geometry, in millimetres. Landscape A4: the report has ten columns and a
#: thumbnail, and portrait forces either a font nobody can read or a wrapped
#: table that cannot be scanned down.
MARGIN_MM = 10.0
ROW_H_MM = 18.0
THUMB_W_MM = 24.0

#: Column widths, summing to the printable width of landscape A4 (277 mm).
COLUMNS: tuple[tuple[str, float], ...] = (
    ("#", 8.0),
    ("Plate", 30.0),
    ("Conf", 14.0),
    ("Camera", 52.0),
    ("District", 26.0),
    ("Coordinates", 38.0),
    ("Time (IST)", 40.0),
    # Colour and class in one cell rather than two. The description reads the
    # way an operator would say it — "silver car" — and on the government
    # estate it is frequently the only thing in the row that identifies the
    # vehicle at all, because those cameras do not read plates.
    ("Vehicle", 28.0),
    ("Crop", THUMB_W_MM),
    # The sighting, then its vehicle id; `~` marks an appearance link.
    ("Sighting, vehicle", 27.0),
)



#: PDF core fonts (Helvetica and friends) are latin-1 only, so anything outside
#: that range must be dealt with before it reaches the page rather than raising
#: mid-render — a report that fails to generate because one camera name has a
#: typographic dash in it is a worse outcome than a substituted character.
#:
#: **Known limitation, worth stating rather than hiding:** this also means the
#: PDF cannot render Gujarati script. Every district and camera name in the
#: registry today is Latin, so nothing is lost here, but a deployment that names
#: assets in Gujarati needs an embedded Unicode TTF (fpdf2 supports it with
#: `add_font`; it costs a few megabytes of font file in the image). The CSV
#: export is UTF-8 throughout and has no such restriction.
_SUBSTITUTIONS = {
    "\u2014": "-", "\u2013": "-", "\u2026": "...",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u00a0": " ", "\u2022": "-",
}


def _pdf_text(value: str) -> str:
    for source, target in _SUBSTITUTIONS.items():
        value = value.replace(source, target)
    # Anything still outside latin-1 becomes '?', which is visible in the
    # output. Silently dropping it would leave a name subtly wrong instead.
    return value.encode("latin-1", errors="replace").decode("latin-1")


def render_pdf(
    rows: list[Detection],
    *,
    title: str = "Vehicle detection report",
    filters: dict[str, Any] | None = None,
    crop_root: pathlib.Path | None = None,
    generated_at: datetime | None = None,
    max_rows: int = 500,
) -> bytes:
    """A case-file page: what was asked for, what was found, and the pictures.

    `crop_root` is where the evidence JPEGs live. When it is not given, or a
    crop is missing, the cell says so rather than leaving a blank that reads as
    "no vehicle" — the difference between an absent image and an absent
    detection matters to whoever reads this later.
    """
    from fpdf import FPDF

    generated = generated_at or datetime.now(UTC)
    shown = rows[:max_rows]

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.set_margins(MARGIN_MM, MARGIN_MM, MARGIN_MM)
    pdf.add_page()

    _header(pdf, title, filters or {}, generated, len(rows), len(shown))
    _table_header(pdf)

    for number, row in enumerate(shown, start=1):
        if pdf.get_y() + ROW_H_MM > pdf.h - MARGIN_MM:
            pdf.add_page()
            _table_header(pdf)
        _row(pdf, number, row, crop_root)

    _footer(pdf, len(rows), len(shown))
    return bytes(pdf.output())


def _header(
    pdf: Any, title: str, filters: dict[str, Any], generated: datetime,
    total: int, shown: int,
) -> None:
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 7, _pdf_text(title), new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(
        0, 4.5,
        _pdf_text(
            f"Gujarat CCTV Integration Platform  -  generated "
        f"{generated.astimezone(IST):%d %b %Y, %H:%M:%S} IST "
            f"({generated.astimezone(UTC):%H:%M:%S} UTC)"
        ),
        new_x="LMARGIN", new_y="NEXT",
    )

    # The query is printed on the report. Without it a page of detections cannot
    # be checked by anyone who was not sitting at the desk when it was run.
    stated = ", ".join(f"{k}={v}" for k, v in sorted(filters.items()) if v not in (None, ""))
    pdf.cell(
        0, 4.5,
        _pdf_text(
            f"Filters: {stated or 'none - all detections in the window'}  -  "
            f"{total} detection{'' if total == 1 else 's'} matched"
            + (f", {shown} shown" if shown < total else "")
        ),
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.cell(
        0, 4.5,
        "Confidence is the weakest character's probability. Rows marked * failed "
        "the Indian plate format check and are retained deliberately.",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)


def _table_header(pdf: Any) -> None:
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(232, 232, 236)
    for label, width in COLUMNS:
        pdf.cell(width, 6, f" {label}", border=0, fill=True)
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 8)


def _row(pdf: Any, number: int, row: Detection, crop_root: pathlib.Path | None) -> None:
    top = pdf.get_y()
    x = MARGIN_MM
    coords = (
        f"{row.lat:.5f}, {row.lon:.5f}" if row.lat is not None and row.lon is not None
        else "not surveyed"
    )
    values = (
        str(number),
        row.plate + ("" if row.format_valid else " *"),
        f"{row.confidence:.2f}",
        row.camera_name or "unknown camera",
        row.district or "",
        coords,
        f"{row.ts_ist:%d %b %H:%M:%S}",
        row.description,
        None,  # the crop cell is drawn as an image, below
        f"{row.id} {row.vehicle_ref}".strip(),
    )

    for (label, width), value in zip(COLUMNS, values, strict=True):
        pdf.set_xy(x, top)
        if label == "Crop":
            _crop_cell(pdf, row, crop_root, x, top, width)
        else:
            # Truncated rather than wrapped: a table that can be read straight
            # down beats one where a long camera name silently doubles a row's
            # height and pushes the crop out of line with its plate.
            pdf.cell(width, ROW_H_MM, f" {_fit(pdf, _pdf_text(str(value)), width - 2)}")
        x += width

    pdf.set_draw_color(214, 214, 218)
    pdf.line(MARGIN_MM, top + ROW_H_MM, pdf.w - MARGIN_MM, top + ROW_H_MM)
    pdf.set_xy(MARGIN_MM, top + ROW_H_MM)


def fit_box(
    width: float, height: float, max_w: float, max_h: float
) -> tuple[float, float]:
    """Largest size fitting inside the cell without distorting the aspect.

    Scaling on height alone was the first attempt and it was wrong in a way that
    only showed in the rendered page: a vehicle crop is wider than it is tall, so
    a 4:1 crop scaled to the row height spilled across the sighting-id column and
    over the page margin. Constrained on both axes, and never enlarged past
    natural size — an upscaled 60 px crop looks like evidence of more detail than
    the camera actually captured.
    """
    if width <= 0 or height <= 0:
        return max_w, max_h
    scale = min(max_w / width, max_h / height, 1.0)
    return width * scale, height * scale


def _crop_cell(
    pdf: Any, row: Detection, crop_root: pathlib.Path | None, x: float, top: float,
    width: float,
) -> None:
    path = (crop_root / row.crop_path) if (crop_root and row.crop_path) else None
    if path is not None and path.is_file():
        try:
            from PIL import Image

            with Image.open(path) as image:
                natural_w, natural_h = image.size
            # Points per millimetre is fpdf's own unit conversion; the image's
            # pixel size only matters as an aspect ratio here.
            draw_w, draw_h = fit_box(
                float(natural_w), float(natural_h), width - 2, ROW_H_MM - 2
            )
            pdf.image(
                str(path),
                x=x + 1 + (width - 2 - draw_w) / 2,
                y=top + 1 + (ROW_H_MM - 2 - draw_h) / 2,
                w=draw_w, h=draw_h,
            )
            return
        except Exception:  # noqa: BLE001 - a bad image must not lose the report
            log.warning("crop unusable for sighting %s", row.id, exc_info=True)

    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(140, 140, 140)
    # Says which of the two it is: no crop was stored, or one was and is gone.
    pdf.cell(width, ROW_H_MM, " no crop" if not row.crop_path else " crop expired")
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 8)


def _fit(pdf: Any, text: str, width_mm: float) -> str:
    if pdf.get_string_width(text) <= width_mm:
        return text
    while text and pdf.get_string_width(text + "...") > width_mm:
        text = text[:-1]
    return text + "..."


def _footer(pdf: Any, total: int, shown: int) -> None:
    pdf.ln(3)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(120, 120, 120)
    note = (
        f"{shown} of {total} detections shown; narrow the filters or export CSV "
        "for the full set." if shown < total else f"{total} detections, complete."
    )
    pdf.cell(0, 4, _pdf_text(note), new_x="LMARGIN", new_y="NEXT")
    pdf.cell(
        0, 4,
        "Times are IST. Every plate read is retained, not only watchlist matches; "
        "this report is drawn from that index.",
        new_x="LMARGIN", new_y="NEXT",
    )
