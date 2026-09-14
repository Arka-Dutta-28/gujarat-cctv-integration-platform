"""Liveness and platform status."""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from services.common.db import fetch_all, fetch_one

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: str = Field(description="`ok` when the API and database both answer.")
    database: str = Field(description="`ok`, or the error if the database is unreachable.")
    uptime_s: float = Field(description="Seconds since this API process started.")


class PlatformStatus(BaseModel):
    """Backs the 'running since HH:MM, N sightings indexed' line in the UI.

    That claim is the visible proof that the sightings index predates the plate
    handed over at evaluation time — which is the entire argument for persisting
    every read rather than only watchlist hits.
    """

    started_at: float
    uptime_s: float
    cameras_total: int
    cameras_online: int
    sightings_total: int
    earliest_sighting: str | None
    latest_sighting: str | None
    #: The **database's** clock, in UTC. Every timestamp the platform stores is
    #: taken from it, so a client comparing its own clock against a sighting or
    #: an alert is comparing against the wrong reference — which for the alerting
    #: latency figure is the difference between a measurement and a guess.
    now: str


_STARTED = time.time()


@router.get(
    "/health",
    response_model=Health,
    summary="Liveness probe",
    description="Returns `ok` when the API process is up and the database answers.",
)
def health() -> Health:
    try:
        fetch_one("SELECT 1 AS ok")
        db = "ok"
        status = "ok"
    except Exception as exc:  # noqa: BLE001 - report, never crash the probe
        db = str(exc)[:200]
        status = "degraded"
    return Health(status=status, database=db, uptime_s=time.time() - _STARTED)


@router.get(
    "/api/status",
    response_model=PlatformStatus,
    summary="Platform status and index coverage",
    description=(
        "Camera counts and the span of the sightings index. The earliest "
        "sighting timestamp is the evidence that the platform was ingesting "
        "before the designated plate was supplied."
    ),
)
def status() -> PlatformStatus:
    cams = fetch_one(
        "SELECT COUNT(*) AS total,"
        " COUNT(*) FILTER (WHERE status = 'online') AS online"
        " FROM cameras"
    ) or {"total": 0, "online": 0}

    # The sightings hypertable may be empty until M3; report zeros, not an error.
    try:
        s = fetch_one(
            "SELECT COUNT(*) AS total, MIN(ts) AS earliest, MAX(ts) AS latest FROM sightings"
        ) or {}
    except Exception:  # noqa: BLE001
        s = {}

    clock = fetch_one("SELECT now() AS now") or {}

    return PlatformStatus(
        started_at=_STARTED,
        uptime_s=time.time() - _STARTED,
        cameras_total=cams["total"],
        cameras_online=cams["online"],
        sightings_total=s.get("total") or 0,
        earliest_sighting=s["earliest"].isoformat() if s.get("earliest") else None,
        latest_sighting=s["latest"].isoformat() if s.get("latest") else None,
        now=clock["now"].isoformat(),
    )


# --- camera tamper (M9) ---------------------------------------------------


class TamperEvent(BaseModel):
    id: int
    camera_id: str
    camera_name: str | None = None
    district: str | None = None
    ts: str
    kind: str = Field(description="covered | defocused | moved")
    detail: str = Field(description="The measurement behind the suspicion, in words.")
    value: float | None = None
    acknowledged_by: str | None = None
    acknowledged_at: str | None = None
    resolution: str | None = None


@router.get(
    "/api/cameras/tamper-events",
    response_model=list[TamperEvent],
    tags=["cameras"],
    summary="Suspected camera interference",
    description=(
        "Cameras that appear to have been covered, defocused or moved, detected "
        "from frames the ANPR pipeline had already decoded.\n\n"
        "This exists because none of the platform's other checks can see it. A "
        "camera under a bag is reachable, decodes cleanly and increments its "
        "frame counter — it is *up*, and it is useless. On an estate of 80,000 "
        "nobody walks past most of them to notice.\n\n"
        "Each event carries the measurement that produced it — grey standard "
        "deviation, Laplacian variance or histogram correlation — because a "
        "threshold an operator cannot inspect is one they cannot dispute. Every "
        "threshold here is wrong for some camera: a night scene has less "
        "contrast than a daylit one, and a PTZ camera moves because that is its "
        "job. Nothing is acted on automatically."
    ),
)
def tamper_events(
    kind: Annotated[str | None, Query(description="covered | defocused | moved")] = None,
    unacknowledged: Annotated[bool, Query()] = False,
    hours: Annotated[int, Query(ge=1, le=720)] = 24,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[TamperEvent]:
    where = ["e.ts > now() - make_interval(hours => %(hours)s)"]
    params: dict[str, Any] = {"hours": hours, "limit": limit}
    if kind:
        where.append("e.kind = %(kind)s")
        params["kind"] = kind
    if unacknowledged:
        where.append("e.acknowledged_at IS NULL")

    rows = fetch_all(
        "SELECT e.id, e.camera_id::text AS camera_id, c.name AS camera_name,"
        " c.district, e.ts, e.kind, e.detail, e.value, e.acknowledged_by,"
        " e.acknowledged_at, e.resolution"
        " FROM camera_tamper_events e JOIN cameras c ON c.id = e.camera_id"
        " WHERE " + " AND ".join(where) + " ORDER BY e.ts DESC LIMIT %(limit)s",
        params,
    )
    return [
        TamperEvent(
            **{
                **r,
                "ts": r["ts"].isoformat(),
                "acknowledged_at": (
                    r["acknowledged_at"].isoformat() if r["acknowledged_at"] else None
                ),
            }
        )
        for r in rows
    ]
