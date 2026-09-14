"""Report export — Deliverable 4.

`GET /api/reports/detections` in two formats. The build plan is specific about
the columns (plate, confidence, camera, geolocation, timestamp, vehicle class,
thumbnail) and specific that this is a deliverable rather than a convenience: a
detection an investigator cannot take out of the platform is not evidence, it is
a screenshot.

The filters are deliberately the same ones `/api/sightings` takes, plus `plate`.
An operator who has just traced a vehicle wants that vehicle's movement history
as a document, and needing to learn a second query language to get it would be a
small piece of friction in exactly the wrong place.

Every export is audited as `report.export` with its filters and row count. A
report is a copy of surveillance data leaving the platform, so who took what,
when, and under which case reference is the whole point of having an audit
trail at all.
"""

from __future__ import annotations

import os
import pathlib
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query, Response

from services.common.audit import Action, record
from services.common.db import fetch_all
from services.common.plates import plate_key_variants
from services.reports.detections import Detection, render_csv, render_pdf

router = APIRouter(prefix="/api/reports", tags=["reports"])

CROP_ROOT = pathlib.Path(os.environ.get("ANPR_CROP_ROOT", "/data/crops"))

#: Hard ceiling on one export. Not a page size — a bound on how much of the
#: index one request can pull, so a mistyped filter cannot ask for the whole
#: hypertable and take the API down while an evaluator watches.
MAX_ROWS = 5_000

_SELECT = """
SELECT s.id, s.ts, s.plate_normalised AS plate, s.confidence,
       c.name AS camera_name, c.district,
       ST_Y(c.geom::geometry) AS lat, ST_X(c.geom::geometry) AS lon,
       s.vehicle_class, s.vehicle_colour, s.condition, s.format_valid,
       s.crop_path, s.vehicle_uid, s.uid_via
  FROM sightings s
  JOIN cameras c ON c.id = s.camera_id
"""


def _detections(
    plate: str | None, camera_id: str | None, district: str | None,
    condition: str | None, since: str | None, until: str | None,
    format_valid: bool | None, limit: int, camera_ref: str | None = None,
) -> tuple[list[Detection], dict[str, Any]]:
    where: list[str] = []
    params: dict[str, Any] = {"limit": min(limit, MAX_ROWS)}

    if plate and plate.strip().startswith("#") and plate.strip()[1:].isdigit():
        # `#5120` is a vehicle id, as in the trace (services/anpr/linking.py).
        params["uid"] = int(plate.strip()[1:])
        where.append("s.vehicle_uid = %(uid)s")
    elif plate:
        # The same normalisation the sighting was written with (invariant 3),
        # so a report requested as "GJ 01 AB 1234" finds a read stored as
        # `GJO1AB1234` — the export must not be stricter than the search.
        params["keys"] = plate_key_variants(plate)
        where.append("s.plate_normalised = ANY(%(keys)s)")
    for name, value, expr in (
        ("camera_id", camera_id, "s.camera_id = %(camera_id)s::uuid"),
        ("district", district, "c.district = %(district)s"),
        # A set of cameras by their registry reference, `*` as the wildcard —
        # `sentinel-cam*` is every government grid camera. Same spelling as the
        # worker's ANPR_CAMERA_REFS.
        ("camera_ref", camera_ref.replace("*", "%") if camera_ref else None,
         "c.external_ref LIKE %(camera_ref)s"),
        ("condition", condition, "s.condition = %(condition)s"),
        ("since", since, "s.ts >= %(since)s::timestamptz"),
        ("until", until, "s.ts <= %(until)s::timestamptz"),
    ):
        if value is not None:
            where.append(expr)
            params[name] = value
    if format_valid is not None:
        where.append("s.format_valid = %(format_valid)s")
        params["format_valid"] = format_valid

    sql = (
        _SELECT
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY s.ts DESC LIMIT %(limit)s"
    )
    rows = [Detection(**r) for r in fetch_all(sql, params)]

    stated = {
        "plate": plate, "camera_id": camera_id, "camera_ref": camera_ref, "district": district,
        "condition": condition, "since": since, "until": until,
        "format_valid": format_valid,
    }
    return rows, {k: v for k, v in stated.items() if v is not None}


@router.get(
    "/detections",
    summary="Export detections as CSV or PDF",
    description=(
        "The detection report (Deliverable 4): plate, confidence, camera, "
        "geolocation, timestamp, vehicle class and thumbnail.\n\n"
        "**CSV** is lossless and joinable — full coordinate precision, ISO-8601 "
        "in UTC *and* IST, one row per detection. **PDF** is the case-file "
        "document: local time, the evidence crop, and the filters the report was "
        "run under printed on the page, because a page of detections with no "
        "statement of what was asked for cannot be checked afterwards.\n\n"
        "Reads that failed the Indian plate format are included and marked. They "
        "are retained in the index on purpose — a mis-read wanted vehicle is "
        "worse than a noisy record — and a report that dropped them silently "
        "would misrepresent what the platform saw.\n\n"
        "Audited as `report.export`. Pass `X-Case-Ref` (or `case_ref=`) to bind "
        "the export to an investigation; a browser download cannot set headers, "
        "so both are accepted."
    ),
    responses={
        200: {
            "content": {"text/csv": {}, "application/pdf": {}},
            "description": "The report, as an attachment.",
        }
    },
)
def detections_report(
    format: Annotated[Literal["csv", "pdf"], Query(description="csv | pdf")] = "csv",
    plate: Annotated[
        str | None, Query(description="Full plate; normalised as at write time.")
    ] = None,
    camera_id: Annotated[str | None, Query()] = None,
    camera_ref: Annotated[
        str | None, Query(description="Registry reference; `*` wildcard, e.g. `sentinel-cam*`.")
    ] = None,
    district: Annotated[str | None, Query()] = None,
    condition: Annotated[str | None, Query(description="day | night | glare")] = None,
    since: Annotated[str | None, Query(description="ISO-8601 lower bound on ts.")] = None,
    until: Annotated[str | None, Query(description="ISO-8601 upper bound on ts.")] = None,
    format_valid: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_ROWS)] = 500,
    # Also accepted as query parameters, because this endpoint is reached by a
    # browser download — an `<a href>` that streams a large PDF without holding
    # it in memory and honours the filename below — and a plain navigation
    # cannot set headers. Every export would otherwise be audited as
    # `anonymous` with no case reference, which is exactly the information the
    # trail exists to keep. Same trust level as the headers either way: both are
    # client-supplied until authentication lands in M8.
    actor_q: Annotated[str | None, Query(alias="actor")] = None,
    case_ref_q: Annotated[str | None, Query(alias="case_ref")] = None,
    actor: Annotated[str, Header(alias="X-Actor")] = "anonymous",
    case_ref: Annotated[str | None, Header(alias="X-Case-Ref")] = None,
) -> Response:
    actor = (actor_q or actor or "anonymous").strip()[:120] or "anonymous"
    case_ref = case_ref_q or case_ref
    rows, stated = _detections(
        plate, camera_id, district, condition, since, until, format_valid, limit,
        camera_ref,
    )

    record(
        Action.REPORT_EXPORT, actor, subject=plate, case_ref=case_ref,
        detail={"format": format, "rows": len(rows), "filters": stated},
    )

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    name = f"detections-{plate.lower()}-{stamp}" if plate else f"detections-{stamp}"

    if format == "csv":
        return Response(
            content=render_csv(rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
        )

    title = f"Vehicle detection report — {plate}" if plate else "Vehicle detection report"
    return Response(
        content=render_pdf(rows, title=title, filters=stated, crop_root=CROP_ROOT),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{name}.pdf"'},
    )
