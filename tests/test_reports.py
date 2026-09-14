"""The detection report — Deliverable 4.

What is being protected here is that the document tells the truth about what it
contains. A report is the artifact that leaves the platform and gets read by
someone who was not present when it was run, so an omission here is not a
cosmetic bug: a page of detections with no statement of the query, or with the
low-confidence reads quietly indistinguishable from the certain ones, invites a
conclusion the data does not support.
"""

from __future__ import annotations

import csv
import io
import pathlib
from datetime import UTC, datetime

import pytest

from services.reports.detections import (
    CSV_COLUMNS,
    IST,
    Detection,
    render_csv,
    render_pdf,
)


def detection(**kw: object) -> Detection:
    base: dict = {
        "id": 4242,
        "ts": datetime(2026, 8, 18, 9, 30, 15, tzinfo=UTC),
        "plate": "GJ18TR4321",
        "confidence": 0.91,
        "camera_name": "NH-48 Anand KM43",
        "district": "Anand",
        "lat": 22.683412,
        "lon": 72.847597,
        "vehicle_class": "car",
        "condition": "day",
        "format_valid": True,
        "crop_path": None,
    }
    base.update(kw)
    return Detection(**base)  # type: ignore[arg-type]


def pdf_text(data: bytes) -> str:
    """Readable text from a rendered PDF.

    The content streams are Flate-compressed, which is right for the artifact
    and inconvenient for a test. Inflating them here rather than turning
    compression off keeps the assertions pointed at the *real* document — a
    report generated specially for the test proves less than the one an
    operator downloads.
    """
    import re
    import zlib

    out = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        chunk = match.group(1)
        try:
            out.append(zlib.decompress(chunk).decode("latin-1", errors="ignore"))
        except zlib.error:
            out.append(chunk.decode("latin-1", errors="ignore"))
    # PDF text is emitted in (…) Tj operators; joining is enough to search.
    return "\n".join(out)


def rows_of(data: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


class TestCsv:
    def test_the_columns_the_build_plan_names_are_all_present(self) -> None:
        """plate, confidence, camera, geolocation, timestamp, class, thumbnail."""
        row = rows_of(render_csv([detection(crop_path="2026/08/18/cam/t.jpg")]))[0]
        assert row["plate"] == "GJ18TR4321"
        assert row["confidence"] == "0.9100"
        assert row["camera"] == "NH-48 Anand KM43"
        assert (row["latitude"], row["longitude"]) == ("22.683412", "72.847597")
        assert row["vehicle_class"] == "car"
        assert row["thumbnail"] == "/api/sightings/4242/crop"
        assert set(CSV_COLUMNS) == set(row)

    def test_both_time_zones_are_carried(self) -> None:
        """Stored UTC, read in Gujarat. One alone is unusable or ambiguous."""
        row = rows_of(render_csv([detection()]))[0]
        assert row["ts_utc"].startswith("2026-08-18T09:30:15+00:00")
        assert row["ts_ist"].startswith("2026-08-18T15:00:15+05:30")

    def test_coordinates_keep_full_precision(self) -> None:
        """This column gets joined against other data; rounding is destructive."""
        row = rows_of(render_csv([detection(lat=22.123456, lon=72.987654)]))[0]
        assert row["latitude"] == "22.123456"

    def test_an_unsurveyed_camera_leaves_coordinates_empty_not_zero(self) -> None:
        """0,0 is in the Gulf of Guinea and would plot as a real detection."""
        row = rows_of(render_csv([detection(lat=None, lon=None)]))[0]
        assert row["latitude"] == ""
        assert row["longitude"] == ""

    def test_a_format_invalid_read_is_exported_and_marked(self) -> None:
        """Kept in the index on purpose; a report that dropped them would lie."""
        row = rows_of(render_csv([detection(plate="GJ18TR43", format_valid=False)]))[0]
        assert row["plate"] == "GJ18TR43"
        assert row["format_valid"] == "false"

    def test_a_sighting_with_no_crop_has_an_empty_thumbnail(self) -> None:
        assert rows_of(render_csv([detection()]))[0]["thumbnail"] == ""

    def test_the_file_opens_correctly_in_a_spreadsheet(self) -> None:
        """A UTF-8 BOM. Without it Excel reads district names as the codepage."""
        assert render_csv([detection()]).startswith(b"\xef\xbb\xbf")

    def test_an_empty_result_still_produces_a_header(self) -> None:
        """A zero-row export is an answer; a zero-byte file looks like a fault."""
        data = render_csv([])
        assert data.decode("utf-8-sig").strip() == ",".join(CSV_COLUMNS)


class TestPdf:
    def test_it_is_a_pdf(self) -> None:
        data = render_pdf([detection()])
        assert data.startswith(b"%PDF-")
        assert b"%%EOF" in data[-1024:]

    def test_the_detection_and_its_timestamp_are_on_the_page(self) -> None:
        """The M6 acceptance in one test: real detections, with their times."""
        data = render_pdf([detection()], filters={"plate": "GJ18TR4321"})
        text = pdf_text(data)
        assert "GJ18TR4321" in text
        # 09:30:15 UTC is 15:00:15 IST, and IST is what the page shows.
        assert "15:00:15" in text

    def test_the_query_is_printed_on_the_report(self) -> None:
        """Otherwise nobody who was not at the desk can check what it contains."""
        text = pdf_text(render_pdf(
            [detection()], filters={"district": "Anand", "condition": "day"}
        ))
        assert "district=Anand" in text
        assert "condition=day" in text

    def test_a_report_with_no_filters_says_so_rather_than_leaving_it_blank(self) -> None:
        text = pdf_text(render_pdf([detection()]))
        assert "none" in text

    def test_a_truncated_report_says_how_much_it_is_hiding(self) -> None:
        """A page that silently shows 500 of 4,000 misrepresents the search."""
        text = pdf_text(render_pdf([detection(id=n) for n in range(20)], max_rows=5))
        assert "5 of 20" in text

    def test_a_missing_crop_is_labelled_rather_than_left_blank(self) -> None:
        """An absent image must not read as an absent detection."""
        text = pdf_text(render_pdf([detection()]))
        assert "no crop" in text

    def test_a_crop_that_was_stored_but_is_gone_says_something_different(self) -> None:
        text = pdf_text(render_pdf(
            [detection(crop_path="2026/08/18/cam/missing.jpg")],
            crop_root=pathlib.Path("/nonexistent"),
        ))
        assert "crop expired" in text

    def test_an_empty_report_still_renders(self) -> None:
        """Zero detections is a finding — "we looked and there were none"."""
        data = render_pdf([], filters={"plate": "GJ99ZZ0000"})
        assert data.startswith(b"%PDF-")
        assert "0 detection" in pdf_text(data)

    @pytest.mark.parametrize("count", [1, 12, 40])
    def test_it_paginates_without_losing_rows(self, count: int) -> None:
        """Rows must not fall off the bottom of a page silently."""
        data = render_pdf([detection(id=n, plate=f"GJ01AB{n:04d}") for n in range(count)])
        text = pdf_text(data)
        assert f"{count} detections, complete." in text

    def test_a_real_crop_is_embedded(self, tmp_path: pathlib.Path) -> None:
        """The thumbnail is a named column of the deliverable, not a nicety.

        Uses Pillow rather than the pipeline's own encoder so this runs
        everywhere: OpenCV lives in the ANPR image, and a test that skipped
        outside it would leave the deliverable's headline feature unproven in
        the suite that actually gets run.
        """
        image_lib = pytest.importorskip("PIL.Image")
        buffer = io.BytesIO()
        image_lib.new("RGB", (120, 60), (128, 128, 128)).save(buffer, format="JPEG")
        data = buffer.getvalue()
        rel = "2026/08/18/cam-01/track-7.jpg"
        target = tmp_path / rel
        target.parent.mkdir(parents=True)
        target.write_bytes(data)

        with_image = render_pdf([detection(crop_path=rel)], crop_root=tmp_path)
        without = render_pdf([detection()], crop_root=tmp_path)
        assert len(with_image) > len(without) + 500
        assert b"DCTDecode" in with_image, "the JPEG should pass through undecoded"


class TestIst:
    def test_ist_is_five_and_a_half_hours_ahead(self) -> None:
        assert detection().ts_ist.utcoffset().total_seconds() == 5.5 * 3600
        assert IST.tzname(None) == "IST"


class TestCropFitting:
    """A crop must stay inside its cell.

    Scaling on height alone passed every test and was still wrong: a vehicle
    crop is much wider than it is tall, so at row height it spilled across the
    sighting-id column and off the page. The defect was only visible in the
    rendered page, which is why the geometry is now a function with its own
    tests rather than three arguments inside a draw call.
    """

    def test_a_wide_crop_is_bounded_by_the_column(self) -> None:
        from services.reports.detections import fit_box

        w, h = fit_box(480, 120, max_w=22, max_h=16)
        assert w <= 22 and h <= 16
        assert w / h == pytest.approx(4.0), "aspect ratio preserved"

    def test_a_tall_crop_is_bounded_by_the_row(self) -> None:
        from services.reports.detections import fit_box

        w, h = fit_box(100, 400, max_w=22, max_h=16)
        assert w <= 22 and h <= 16
        assert h == pytest.approx(16)

    def test_a_small_crop_is_not_enlarged(self) -> None:
        """Upscaling implies detail the camera did not capture."""
        from services.reports.detections import fit_box

        assert fit_box(10, 6, max_w=22, max_h=16) == (10, 6)

    def test_a_degenerate_size_falls_back_to_the_cell(self) -> None:
        from services.reports.detections import fit_box

        assert fit_box(0, 0, max_w=22, max_h=16) == (22, 16)


class TestVehicleDescription:
    """Colour in the report, which is what carries Deliverable 4.

    The government estate produced 20 valid plates from 4,442 reads over 45
    minutes. A report of those rows alone is thin. The same report with a
    described vehicle on every row is evidence of a working pipeline, which is
    the claim the deliverable actually has to support.
    """

    def test_the_colour_is_a_csv_column(self) -> None:
        assert "vehicle_colour" in CSV_COLUMNS

    def test_the_description_reads_the_way_an_operator_would_say_it(self) -> None:
        assert detection(vehicle_colour="silver", vehicle_class="car").description == (
            "silver car"
        )

    def test_a_row_with_no_colour_still_names_the_class(self) -> None:
        assert detection(vehicle_colour=None, vehicle_class="truck").description == "truck"

    def test_nothing_known_prints_blank_rather_than_unknown(self) -> None:
        """A blank cell reads as an absence. "unknown" printed 200 times reads
        as a fault in the platform."""
        assert detection(vehicle_colour=None, vehicle_class=None).description == ""


def test_a_set_of_cameras_is_selected_by_reference_with_a_wildcard(monkeypatch) -> None:  # noqa: ANN001
    """`camera_ref=sentinel-cam*` exports every government grid camera at once."""
    from services.api.routers import reports

    seen = {}
    monkeypatch.setattr(reports, "fetch_all",
                        lambda sql, params: seen.update(sql=sql, params=params) or [])
    rows, stated = reports._detections(None, None, None, None, None, None, None, 100,
                                        camera_ref="sentinel-cam*")
    assert rows == [] and stated == {"camera_ref": "sentinel-cam*"}
    assert "c.external_ref LIKE %(camera_ref)s" in seen["sql"]
    assert seen["params"]["camera_ref"] == "sentinel-cam%"
