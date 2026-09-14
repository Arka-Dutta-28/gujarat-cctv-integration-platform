"""Watchlist: the vehicles the platform is actively looking for.

This is the input to the live alerting path (M5), and it is deliberately small
and boring: a plate, why it is wanted, how serious it is, and whether the entry
is still active. The interesting behaviour is in services/alerting/, which
matches every new sighting against it as the sighting is written.

Two things here are not obvious.

Plates are normalised on the way in, with the same function that normalises a
read (normalise_plate, invariant 3). An operator typing "GJ 01 AB 1234" and a
camera reading "GJO1AB1234" must land on the same key, or the alert never fires,
and this is the sort of mismatch that shows up in a live demo rather than in a
test.

Entries are deactivated, never deleted. An alert holds a foreign key to the
watchlist row that raised it; deleting the entry would either fail or orphan the
evidence for an action a police officer took. active=false stops future matching
and leaves the history intact.
"""

from __future__ import annotations

import csv
import io
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, field_validator

from services.api.routers.cameras import Writer
from services.common.audit import Action, record
from services.common.db import connection, fetch_all, fetch_one
from services.common.plates import is_valid_format, normalise_plate

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

CATEGORIES = ("stolen", "wanted", "missing", "blacklisted", "suspect", "other")
Category = Literal["stolen", "wanted", "missing", "blacklisted", "suspect", "other"]


class WatchlistEntry(BaseModel):
    id: str
    plate: str = Field(description="As entered by the operator.")
    plate_normalised: str = Field(description="The key alerting matches on.")
    category: str
    severity: int
    source: str | None = None
    case_ref: str | None = None
    notes: str | None = None
    active: bool
    created_at: str
    #: Filled by the listing query — how often this entry has actually fired.
    alerts: int = 0


class WatchlistCreate(BaseModel):
    plate: Annotated[str, Field(min_length=2, max_length=20)]
    category: Category = "wanted"
    severity: Annotated[int, Field(ge=1, le=5)] = 3
    source: str | None = Field(
        None, description="VAHAN | eGujCop | manual | representative"
    )
    case_ref: str | None = None
    notes: str | None = None

    @field_validator("plate")
    @classmethod
    def _cleaned(cls, v: str) -> str:
        return v.strip().upper()


class WatchlistUpdate(BaseModel):
    category: Category | None = None
    severity: Annotated[int | None, Field(ge=1, le=5)] = None
    case_ref: str | None = None
    notes: str | None = None
    active: bool | None = None


_SELECT = """
SELECT w.id::text AS id, w.plate, w.plate_normalised, w.category::text AS category,
       w.severity, w.source, w.case_ref, w.notes, w.active, w.created_at,
       count(a.id)::int AS alerts
  FROM watchlist w
  LEFT JOIN alerts a ON a.watchlist_id = w.id
"""
_GROUP = " GROUP BY w.id"


def _entry(row: dict[str, Any]) -> WatchlistEntry:
    return WatchlistEntry(**{**row, "created_at": row["created_at"].isoformat()})


@router.get(
    "",
    response_model=list[WatchlistEntry],
    summary="List watchlist entries",
    description=(
        "Newest first. `alerts` counts how many times each entry has actually "
        "fired, which is what distinguishes a live watchlist from a list nobody "
        "has checked."
    ),
)
def list_watchlist(
    active: Annotated[bool | None, Query()] = None,
    category: Annotated[Category | None, Query()] = None,
    q: Annotated[str | None, Query(description="Plate fragment.")] = None,
) -> list[WatchlistEntry]:
    where, params = [], {}
    if active is not None:
        where.append("w.active = %(active)s")
        params["active"] = active
    if category is not None:
        where.append("w.category = %(category)s::wl_category")
        params["category"] = category
    if q:
        where.append("w.plate_normalised LIKE %(q)s")
        params["q"] = f"%{normalise_plate(q)}%"

    sql = _SELECT + (" WHERE " + " AND ".join(where) if where else "") + _GROUP
    return [_entry(r) for r in fetch_all(sql + " ORDER BY w.created_at DESC", params)]


@router.post(
    "",
    response_model=WatchlistEntry,
    status_code=201,
    summary="Add a plate to the watchlist",
    description=(
        "The plate is normalised with the same positional rules used when a "
        "sighting is written, so an operator's spacing and a camera's OCR land "
        "on the same key.\n\n"
        "Adding an entry arms the live alerting path: every ANPR worker picks it "
        "up within its watchlist refresh interval (2 s by default) and the next "
        "sighting of that vehicle raises an alert. Audited as `watchlist.change`."
    ),
)
def add_to_watchlist(
    who: Writer, payload: Annotated[WatchlistCreate, Body()]
) -> WatchlistEntry:
    key = normalise_plate(payload.plate)
    if not key:
        raise HTTPException(status_code=422, detail="plate normalises to nothing")

    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO watchlist (plate, plate_normalised, category, severity,"
            " source, case_ref, notes)"
            " VALUES (%s, %s, %s::wl_category, %s, %s, %s, %s) RETURNING id::text",
            (payload.plate, key, payload.category, payload.severity,
             payload.source or "manual", payload.case_ref, payload.notes),
        )
        new_id = cur.fetchone()["id"]

    record(
        Action.WATCHLIST_CHANGE, who, subject=key, case_ref=payload.case_ref,
        detail={"op": "add", "category": payload.category,
                "severity": payload.severity,
                # A plate that fails the Indian format check is still accepted —
                # an out-of-state or partially known registration is exactly the
                # kind of entry an investigator needs — but it is recorded, so a
                # watchlist that never fires can be explained.
                "format_valid": is_valid_format(payload.plate)},
    )
    row = fetch_one(_SELECT + " WHERE w.id = %(id)s" + _GROUP, {"id": new_id})
    assert row is not None
    return _entry(row)


@router.patch(
    "/{entry_id}",
    response_model=WatchlistEntry,
    summary="Amend or deactivate an entry",
    description=(
        "Setting `active: false` is how an entry is retired. There is no delete: "
        "alerts hold a foreign key to the entry that raised them, and erasing it "
        "would orphan the evidence for an action an officer took."
    ),
    responses={404: {"description": "No such entry."}},
)
def update_watchlist(
    entry_id: str, who: Writer, payload: Annotated[WatchlistUpdate, Body()]
) -> WatchlistEntry:
    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")

    sets, params = [], {"id": entry_id}
    for name, value in fields.items():
        sets.append(
            f"{name} = %({name})s::wl_category" if name == "category"
            else f"{name} = %({name})s"
        )
        params[name] = value

    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE watchlist SET {', '.join(sets)} WHERE id = %(id)s"
            " RETURNING plate_normalised",
            params,
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="watchlist entry not found")

    record(
        Action.WATCHLIST_CHANGE, who, subject=row["plate_normalised"],
        detail={"op": "update", **fields},
    )
    updated = fetch_one(_SELECT + " WHERE w.id = %(id)s" + _GROUP, {"id": entry_id})
    assert updated is not None
    return _entry(updated)


class ImportResult(BaseModel):
    imported: int
    updated: int
    rejected: list[dict[str, Any]]


@router.post(
    "/import",
    response_model=ImportResult,
    summary="Bulk import a watchlist as CSV",
    description=(
        "Columns: `plate` (required), `category`, `severity`, `source`, "
        "`case_ref`, `notes`. A stolen-vehicle list arrives from VAHAN or "
        "eGujCop as a spreadsheet, not as 400 form submissions.\n\n"
        "All-or-nothing, like the camera import: a half-loaded stolen-vehicle "
        "list is worse than none, because nobody can tell which half is armed. "
        "Re-importing the same plate updates it rather than duplicating it, so a "
        "daily feed can simply be re-uploaded."
    ),
)
async def import_watchlist(
    who: Writer, file: Annotated[UploadFile, File(description="CSV file.")]
) -> ImportResult:
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(raw))

    parsed: list[WatchlistCreate] = []
    rejected: list[dict[str, Any]] = []
    for number, row in enumerate(reader, start=2):  # row 1 is the header
        cleaned = {
            (k or "").strip().lower(): (v or "").strip()
            for k, v in row.items() if k
        }
        if not cleaned.get("plate"):
            rejected.append({"row": number, "error": "missing plate"})
            continue
        try:
            parsed.append(WatchlistCreate(
                plate=cleaned["plate"],
                category=cleaned.get("category") or "wanted",  # type: ignore[arg-type]
                severity=int(cleaned.get("severity") or 3),
                source=cleaned.get("source") or "import",
                case_ref=cleaned.get("case_ref") or None,
                notes=cleaned.get("notes") or None,
            ))
        except Exception as exc:  # noqa: BLE001 - report, do not abort
            rejected.append({"row": number, "error": str(exc)})

    if rejected:
        raise HTTPException(
            status_code=422,
            detail={"message": "no rows imported; fix these and retry",
                    "rejected": rejected},
        )
    if not parsed:
        raise HTTPException(status_code=422, detail="no rows in file")

    imported = updated = 0
    with connection() as conn, conn.cursor() as cur:
        for entry in parsed:
            key = normalise_plate(entry.plate)
            cur.execute("SELECT id FROM watchlist WHERE plate_normalised = %s", (key,))
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    "UPDATE watchlist SET plate = %s, category = %s::wl_category,"
                    " severity = %s, source = %s, case_ref = %s, notes = %s,"
                    " active = true WHERE id = %s",
                    (entry.plate, entry.category, entry.severity, entry.source,
                     entry.case_ref, entry.notes, existing["id"]),
                )
                updated += 1
            else:
                cur.execute(
                    "INSERT INTO watchlist (plate, plate_normalised, category,"
                    " severity, source, case_ref, notes)"
                    " VALUES (%s, %s, %s::wl_category, %s, %s, %s, %s)",
                    (entry.plate, key, entry.category, entry.severity,
                     entry.source, entry.case_ref, entry.notes),
                )
                imported += 1

    record(
        Action.WATCHLIST_CHANGE, who,
        detail={"op": "import", "imported": imported, "updated": updated,
                "filename": file.filename},
    )
    return ImportResult(imported=imported, updated=updated, rejected=[])
