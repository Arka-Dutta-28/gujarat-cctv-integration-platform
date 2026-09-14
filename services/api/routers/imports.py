"""Bulk camera import.

An estate of roughly 80,000 cameras is never onboarded one form at a time.
Departments hold their inventory as spreadsheets, so CSV is the format that
actually arrives.

The import is deliberately all-or-nothing per request and reports every bad row
rather than stopping at the first. A partial import of a department's inventory
is worse than none: it leaves the operator unable to tell which half landed, and
re-running produces duplicates unless every row is keyed. Validating everything
first and committing once removes that whole class of problem.
"""

from __future__ import annotations

import csv
import io
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field, ValidationError

from services.api.routers.cameras import CameraCreate, Writer
from services.common.audit import Action, record
from services.common.db import connection, fetch_all

router = APIRouter(prefix="/api/cameras", tags=["cameras"])

# Accepted spreadsheet headings, mapped onto the create model. Departments label
# their columns differently and normalising here is cheaper than asking every
# department to reformat.
ALIASES: dict[str, str] = {
    "camera_name": "name",
    "location": "name",
    "site": "name",
    "url": "stream_ref",
    "stream": "stream_ref",
    "stream_url": "stream_ref",
    "rtsp_url": "stream_ref",
    "latitude": "lat",
    "longitude": "lon",
    "long": "lon",
    "lng": "lon",
    "type": "kind",
    "camera_type": "kind",
    "fov": "fov_degrees",
    "range": "range_m",
    "dept": "department",
    "owner": "department",
    "ref": "external_ref",
    "id": "external_ref",
    "asset_id": "external_ref",
}

# A department's inventory lists stream URLs, not adapter names — "adapter" is
# our word, not theirs. Infer it from the URL scheme so the spreadsheet they
# already maintain imports unchanged, while still letting an explicit column win.
SCHEME_ADAPTERS = {
    "rtsp": "rtsp",
    "rtsps": "rtsp",
    "http": "http",
    "https": "http",
    "file": "file",
}


def _infer_adapter(stream_ref: str) -> str | None:
    scheme = stream_ref.split("://", 1)[0].lower() if "://" in stream_ref else ""
    if scheme in SCHEME_ADAPTERS:
        # An .m3u8 over HTTP is HLS, which needs a playlist reader rather than a
        # plain byte-range fetch, so it is worth distinguishing here.
        if scheme in {"http", "https"} and ".m3u8" in stream_ref.lower():
            return "hls"
        return SCHEME_ADAPTERS[scheme]
    return None


NUMERIC = {"lat", "lon", "bearing", "fov_degrees", "range_m", "mounting_height_m", "retention_days"}
INT_FIELDS = {"bearing", "fov_degrees", "range_m", "retention_days"}

MAX_ROWS = 10_000


class RowError(BaseModel):
    row: int = Field(description="1-based row number in the file, excluding the header.")
    field: str | None
    message: str


class ImportResult(BaseModel):
    dry_run: bool
    rows_read: int
    imported: int
    updated: int
    errors: list[RowError]
    detected_columns: list[str]


def _normalise_header(name: str) -> str:
    key = name.strip().lower().replace(" ", "_").replace("-", "_")
    return ALIASES.get(key, key)


def _coerce(row: dict[str, str]) -> dict[str, Any]:
    """Turn CSV strings into the types the create model expects."""
    out: dict[str, Any] = {}
    for key, raw in row.items():
        if key is None:
            continue
        value = (raw or "").strip()
        if value == "":
            continue  # blank means "not supplied", not "null it out"
        if key in NUMERIC:
            number = float(value)
            out[key] = int(number) if key in INT_FIELDS else number
        else:
            out[key] = value
    return out


@router.post(
    "/import",
    response_model=ImportResult,
    summary="Bulk import cameras from CSV",
    description=(
        "Upload a CSV of cameras. Required columns: `name`, `stream_ref`, "
        "`lat`, `lon`. The adapter is inferred from the URL scheme "
        "(`rtsp://` → rtsp, `https://…m3u8` → hls, `https://` → http) unless an "
        "`adapter` column says otherwise. Optional: `external_ref`, `department`, "
        "`district`, `address`, `kind`, `bearing`, `fov_degrees`, `range_m`, "
        "`retention_days`.\n\n"
        "Common column spellings are accepted (`latitude`, `rtsp_url`, `site`, "
        "`asset_id`, …) so a department's own spreadsheet usually imports "
        "unchanged.\n\n"
        "**All or nothing.** Every row is validated first; if any row fails, "
        "nothing is written and every error is returned with its row number. "
        "Rows carrying an `external_ref` that already exists are updated rather "
        "than duplicated, so a corrected file can simply be re-uploaded.\n\n"
        "Use `dry_run=true` to validate a file without writing."
    ),
    responses={
        400: {"description": "The file is not readable as CSV, or is too large."},
        422: {"description": "One or more rows failed validation; nothing was written."},
    },
)
async def import_cameras(
    who: Writer,
    file: Annotated[UploadFile, File(description="CSV file, UTF-8.")],
    dry_run: Annotated[bool, Form(description="Validate only; write nothing.")] = False,
) -> ImportResult:
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"file is not UTF-8: {exc}") from exc

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="no header row found")

    reader.fieldnames = [_normalise_header(f) for f in reader.fieldnames]
    detected = list(reader.fieldnames)

    errors: list[RowError] = []
    # Paired with the file's own row number: rows that fail validation are not
    # added, so a position in this list is not a position in the file, and
    # reporting the wrong row number sends the operator to the wrong line.
    parsed: list[tuple[int, CameraCreate]] = []
    rows_read = 0

    for i, row in enumerate(reader, start=1):
        rows_read = i
        if i > MAX_ROWS:
            raise HTTPException(
                status_code=400, detail=f"file exceeds {MAX_ROWS} rows; split it"
            )
        try:
            values = _coerce(row)
            if "adapter" not in values and values.get("stream_ref"):
                inferred = _infer_adapter(str(values["stream_ref"]))
                if inferred:
                    values["adapter"] = inferred
            parsed.append((i, CameraCreate(**values)))
        except ValidationError as exc:
            for err in exc.errors():
                errors.append(
                    RowError(
                        row=i,
                        field=".".join(str(p) for p in err["loc"]) or None,
                        message=err["msg"],
                    )
                )
        except (TypeError, ValueError) as exc:
            errors.append(RowError(row=i, field=None, message=str(exc)))

    # Departments are checked here rather than at insert time so an unknown one
    # is reported with its row number alongside every other problem, instead of
    # surfacing as a lone failure after the rest of the file already validated.
    # Resolving them in one query also avoids a lookup per row.
    wanted = {c.department for _, c in parsed if c.department}
    known: dict[str, int] = {}
    if wanted:
        known = {
            r["name"]: r["id"]
            for r in fetch_all(
                "SELECT id, name FROM departments WHERE name = ANY(%s)", (list(wanted),)
            )
        }
        for row_number, cam in parsed:
            if cam.department and cam.department not in known:
                errors.append(
                    RowError(
                        row=row_number, field="department",
                        message=f"unknown department: {cam.department!r}",
                    )
                )

    if errors:
        # Ordered by row so the operator reads them in file order.
        errors.sort(key=lambda e: (e.row, e.field or ""))
        # Nothing is written. Reported as 422 with every failure, so one upload
        # tells the operator everything to fix rather than one problem per try.
        raise HTTPException(
            status_code=422,
            detail={
                "message": f"{len(errors)} problem(s) in {rows_read} rows; nothing was imported",
                "errors": [e.model_dump() for e in errors],
            },
        )

    if dry_run:
        return ImportResult(
            dry_run=True, rows_read=rows_read, imported=0, updated=0,
            errors=[], detected_columns=detected,
        )

    imported = updated = 0
    # One transaction for the whole file: a half-imported inventory is worse
    # than a rejected one.
    with connection() as conn, conn.cursor() as cur:
        for _row_number, cam in parsed:
            dept_id = known.get(cam.department) if cam.department else None
            cur.execute(
                """
                INSERT INTO cameras (
                    external_ref, name, department_id, ownership_type, adapter,
                    stream_ref, credential_ref, geom, address, district, kind,
                    bearing, fov_degrees, range_m, mounting_height_m,
                    status, retention_days
                ) VALUES (
                    %(external_ref)s, %(name)s, %(department_id)s,
                    %(ownership_type)s::ownership, %(adapter)s::adapter_type,
                    %(stream_ref)s, %(credential_ref)s,
                    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
                    %(address)s, %(district)s, %(kind)s::camera_kind,
                    %(bearing)s, %(fov_degrees)s, %(range_m)s, %(mounting_height_m)s,
                    'unknown'::camera_status, %(retention_days)s
                )
                ON CONFLICT (external_ref) DO UPDATE SET
                    name = EXCLUDED.name,
                    department_id = EXCLUDED.department_id,
                    adapter = EXCLUDED.adapter,
                    stream_ref = EXCLUDED.stream_ref,
                    geom = EXCLUDED.geom,
                    address = EXCLUDED.address,
                    district = EXCLUDED.district,
                    kind = EXCLUDED.kind,
                    bearing = EXCLUDED.bearing,
                    fov_degrees = EXCLUDED.fov_degrees,
                    range_m = EXCLUDED.range_m
                RETURNING (xmax = 0) AS inserted
                """,
                {**cam.model_dump(), "department_id": dept_id},
            )
            result = cur.fetchone()
            # xmax = 0 distinguishes a fresh insert from an upsert, so the
            # operator learns how many rows were new versus corrected.
            if result and result.get("inserted"):
                imported += 1
            else:
                updated += 1

    record(
        Action.CAMERA_IMPORT, who,
        subject=file.filename,
        detail={"rows": rows_read, "imported": imported, "updated": updated},
    )
    return ImportResult(
        dry_run=False, rows_read=rows_read, imported=imported, updated=updated,
        errors=[], detected_columns=detected,
    )
