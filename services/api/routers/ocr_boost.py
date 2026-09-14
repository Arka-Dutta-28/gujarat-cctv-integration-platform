"""OCR boost: read chosen cameras with PaddleOCR-VL, on request, for a while.

The estate reads plates with docTR on the CPU. When an investigation narrows a
vehicle down to a place, the cameras around it are worth reading with the
heavier reader: PaddleOCR-VL found 101 of 101 plates on the generated night
clips against docTR's 63 (14 Sep 2026). It needs a GPU, so it is never on by
default and never estate-wide.

  - Choose cameras by id, or by a point and a radius, meaning the cameras whose
    registered position falls inside it.
  - It always expires, between 1 and 720 minutes. Starting a boost on a camera
    that already has one replaces it.
  - It reports whether it is really running. The ANPR worker that owns the
    camera writes back applied_at, or apply_error when it cannot load the model,
    for example on a worker without a GPU. `status` says which.
  - It is capped (OCR_BOOST_MAX_CAMERAS, default 12 active at once), because
    every boosted crop holds an OCR slot for about 440 ms on the worker it runs
    on, and those slots are shared with every other camera there.

Operators only; audited as camera.ocr_boost.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from services.api.routers.cameras import Writer
from services.common.audit import Action, record
from services.common.db import connection, fetch_all

router = APIRouter(prefix="/api/ocr-boosts", tags=["ocr boost"])

MAX_CAMERAS = int(os.environ.get("OCR_BOOST_MAX_CAMERAS", "12"))
Backend = Literal["paddleocr-vl"]
Status = Literal["pending", "running", "failed", "expired", "cleared"]


class Near(BaseModel):
    lat: Annotated[float, Field(ge=-90, le=90)]
    lon: Annotated[float, Field(ge=-180, le=180)]
    radius_m: Annotated[float, Field(gt=0, le=20_000)] = 1_000


class BoostRequest(BaseModel):
    camera_ids: list[str] | None = Field(None, description="Camera UUIDs.")
    near: Near | None = Field(None, description="Every camera within `radius_m` of the point.")
    backend: Backend = "paddleocr-vl"
    minutes: Annotated[int, Field(ge=1, le=720)] = 60
    case_ref: str | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _one_selector(self) -> BoostRequest:
        if (self.camera_ids is None) == (self.near is None):
            raise ValueError("give exactly one of camera_ids or near")
        if self.camera_ids is not None and not self.camera_ids:
            raise ValueError("camera_ids is empty")
        return self


class Boost(BaseModel):
    id: int
    camera_id: str
    camera_ref: str | None
    camera_name: str
    backend: str
    status: Status
    requested_by: str
    case_ref: str | None
    reason: str | None
    requested_at: str
    expires_at: str
    applied_at: str | None
    apply_error: str | None


def boost_status(row: dict[str, Any], now: datetime) -> Status:
    """What an operator needs to know about one boost, most final state first."""
    if row["cleared_at"] is not None:
        return "cleared"
    if row["expires_at"] <= now:
        return "expired"
    if row["apply_error"]:
        return "failed"
    if row["applied_at"] is not None:
        return "running"
    return "pending"


_SELECT = """
SELECT b.*, b.camera_id::text AS camera_id, c.external_ref AS camera_ref, c.name AS camera_name,
       now() AS db_now
  FROM camera_ocr_boosts b JOIN cameras c ON c.id = b.camera_id
"""
_OPEN = "b.cleared_at IS NULL AND b.expires_at > now()"


def _boost(row: dict[str, Any]) -> Boost:
    iso = {k: (row[k].isoformat() if row[k] else None)
           for k in ("requested_at", "expires_at", "applied_at")}
    return Boost(**{**{k: row[k] for k in Boost.model_fields if k in row}, **iso,
                    "status": boost_status(row, row["db_now"])})


@router.get(
    "",
    response_model=list[Boost],
    summary="List OCR boosts",
    description="Newest first. `active=true` (the default) returns only boosts that "
                "are neither cleared nor expired, whatever their `status`.",
)
def list_boosts(active: Annotated[bool, Query()] = True) -> list[Boost]:
    where = f" WHERE {_OPEN}" if active else ""
    return [_boost(r) for r in fetch_all(_SELECT + where + " ORDER BY b.requested_at DESC")]


@router.post(
    "",
    response_model=list[Boost],
    status_code=201,
    summary="Read chosen cameras with PaddleOCR-VL for a while",
    description=(
        "Pick cameras by `camera_ids`, or by `near` (a point and a radius in "
        "metres). Each gets a boost that expires after `minutes`. The worker "
        "owning a camera swaps its reader within a few seconds and records "
        "whether that worked: watch `status` go from `pending` to `running`, or "
        "to `failed` with `apply_error` (for example, no GPU on that worker). "
        f"At most {MAX_CAMERAS} cameras may be boosted at once. "
        "Audited as `camera.ocr_boost`."
    ),
    responses={404: {"description": "No active camera matched."},
               409: {"description": "Would exceed the active boost cap."}},
)
def start_boost(who: Writer, payload: Annotated[BoostRequest, Body()]) -> list[Boost]:
    if payload.near is not None:
        n = payload.near
        cams = fetch_all(
            "SELECT id::text AS id FROM cameras WHERE status <> 'decommissioned'"
            " AND geom IS NOT NULL AND ST_DWithin(geom,"
            " ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography, %(r)s)",
            {"lon": n.lon, "lat": n.lat, "r": n.radius_m},
        )
    else:
        cams = fetch_all(
            "SELECT id::text AS id FROM cameras WHERE status <> 'decommissioned'"
            " AND id::text = ANY(%(ids)s)",
            {"ids": payload.camera_ids},
        )
    ids = sorted({c["id"] for c in cams})
    if not ids:
        raise HTTPException(status_code=404, detail="no active camera matched")

    with connection() as conn, conn.cursor() as cur:
        # Serialise boost starts, so two operators cannot each pass the cap check.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('camera_ocr_boosts'))")
        cur.execute(
            f"SELECT count(DISTINCT camera_id) AS n FROM camera_ocr_boosts b WHERE {_OPEN}"
            " AND NOT (camera_id::text = ANY(%(ids)s))", {"ids": ids},
        )
        if cur.fetchone()["n"] + len(ids) > MAX_CAMERAS:
            raise HTTPException(
                status_code=409,
                detail=f"{len(ids)} cameras would exceed the cap of {MAX_CAMERAS} boosted "
                       "at once. Narrow the radius, or clear boosts that are done.",
            )
        cur.execute(
            "UPDATE camera_ocr_boosts SET cleared_at = now(), cleared_by = %(who)s"
            " WHERE camera_id::text = ANY(%(ids)s) AND cleared_at IS NULL",
            {"who": f"{who} (replaced)", "ids": ids},
        )
        cur.execute(
            "INSERT INTO camera_ocr_boosts (camera_id, backend, requested_by, case_ref,"
            " reason, expires_at)"
            " SELECT id::uuid, %(backend)s, %(who)s, %(case)s, %(reason)s,"
            " now() + make_interval(mins => %(minutes)s) FROM unnest(%(ids)s::text[]) AS id"
            " RETURNING id",
            {"backend": payload.backend, "who": who, "case": payload.case_ref,
             "reason": payload.reason, "minutes": payload.minutes, "ids": ids},
        )
        new = [r["id"] for r in cur.fetchall()]

    record(
        Action.CAMERA_OCR_BOOST, who, subject=",".join(ids), case_ref=payload.case_ref,
        detail={"op": "start", "backend": payload.backend, "minutes": payload.minutes,
                "cameras": len(ids), "near": payload.near.model_dump() if payload.near else None},
    )
    return [_boost(r) for r in fetch_all(
        _SELECT + " WHERE b.id = ANY(%(new)s) ORDER BY c.name", {"new": new})]


@router.delete(
    "/{camera_id}",
    response_model=list[Boost],
    summary="Stop boosting a camera",
    description="Clears the camera's active boost; its worker returns it to the "
                "default reader within a few seconds. Audited as `camera.ocr_boost`.",
    responses={404: {"description": "That camera has no active boost."}},
)
def clear_boost(camera_id: str, who: Writer) -> list[Boost]:
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE camera_ocr_boosts b SET cleared_at = now(), cleared_by = %(who)s"
            f" WHERE b.camera_id::text = %(cam)s AND {_OPEN} RETURNING id",
            {"who": who, "cam": camera_id},
        )
        cleared = [r["id"] for r in cur.fetchall()]
    if not cleared:
        raise HTTPException(status_code=404, detail="that camera has no active boost")
    record(Action.CAMERA_OCR_BOOST, who, subject=camera_id, detail={"op": "clear"})
    return [_boost(r) for r in fetch_all(_SELECT + " WHERE b.id = ANY(%(ids)s)", {"ids": cleared})]
