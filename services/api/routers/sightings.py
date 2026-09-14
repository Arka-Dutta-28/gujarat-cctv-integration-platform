"""Sightings — the index the whole design rests on.

Every plate read is here, not only watchlist matches. That is invariant 1,
invariant and the reason the platform can answer a question about a vehicle that
drove past hours before anyone knew to look for it.

Search is exposed as a *plain listing plus filters* here; the retrospective
trace and journey reconstruction are M4 and live in their own router, because
trace and live alerting are separate code paths and must stay that way.
"""

from __future__ import annotations

import os
import pathlib
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from services.common.audit import Action, record
from services.common.db import fetch_all, fetch_one
from services.common.plates import plate_key_variants

router = APIRouter(prefix="/api/sightings", tags=["sightings"])


class Sighting(BaseModel):
    id: int
    ts: str = Field(description="Ingest time, UTC. Never the camera's burnt-in clock.")
    camera_id: str
    camera_name: str | None = None
    district: str | None = None
    lat: float | None = None
    lon: float | None = None
    plate_raw: str
    plate_normalised: str
    confidence: float
    format_valid: bool
    vehicle_class: str | None = None
    vehicle_colour: str | None = Field(
        default=None,
        description=(
            "What a person would call this vehicle's colour. Null means the "
            "platform declined to name one — a crop too small, too dark or too "
            "mixed to support a name — never 'no colour'."
        ),
    )
    colour_confidence: float | None = None
    condition: str | None = None
    read_count: int | None = None
    track_frames: int | None = None
    slot_offset: float | None = None
    bbox: list[int] | None = None
    plate_bbox: list[int] | None = None


_SELECT = """
SELECT s.id, s.ts, s.camera_id::text AS camera_id, c.name AS camera_name,
       c.district, ST_Y(c.geom::geometry) AS lat, ST_X(c.geom::geometry) AS lon,
       s.plate_raw, s.plate_normalised, s.confidence, s.format_valid,
       s.vehicle_class, s.vehicle_colour, s.colour_confidence,
       s.condition, s.read_count, s.track_frames,
       s.slot_offset, s.bbox, s.plate_bbox
  FROM sightings s
  JOIN cameras c ON c.id = s.camera_id
"""


def _rows(sql: str, params: dict[str, Any]) -> list[Sighting]:
    return [
        Sighting(**{**r, "ts": r["ts"].isoformat()}) for r in fetch_all(sql, params)
    ]


@router.get(
    "",
    response_model=list[Sighting],
    summary="List sightings",
    description=(
        "Newest first. Filter by camera, district, condition or time window.\n\n"
        "This is the raw index: **every** plate read the platform has made, "
        "including reads that failed the Indian format check (`format_valid: "
        "false`). Those are kept deliberately — a mis-read wanted vehicle is "
        "worse than a noisy record."
    ),
)
def list_sightings(
    camera_id: Annotated[str | None, Query()] = None,
    district: Annotated[str | None, Query()] = None,
    condition: Annotated[str | None, Query(description="day | night | glare")] = None,
    since: Annotated[str | None, Query(description="ISO-8601 lower bound on ts.")] = None,
    until: Annotated[str | None, Query(description="ISO-8601 upper bound on ts.")] = None,
    format_valid: Annotated[bool | None, Query()] = None,
    colour: Annotated[str | None, Query(
        description=(
            "Vehicle colour: white | silver | grey | black | red | blue | "
            "brown | orange | yellow | green | purple."
        ),
    )] = None,
    unread_only: Annotated[bool, Query(
        description=(
            "Only vehicles that were seen and described but never read. These "
            "are what the wide-area cameras produce — 0 of the 30 government "
            "cameras reach ANPR grade — and they carry an empty plate."
        ),
    )] = False,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[Sighting]:
    where, params = [], {"limit": limit}
    for column, value, expr in (
        ("camera_id", camera_id, "s.camera_id = %(camera_id)s::uuid"),
        ("district", district, "c.district = %(district)s"),
        ("condition", condition, "s.condition = %(condition)s"),
        ("colour", colour, "s.vehicle_colour = %(colour)s"),
        ("since", since, "s.ts >= %(since)s::timestamptz"),
        ("until", until, "s.ts <= %(until)s::timestamptz"),
    ):
        if value is not None:
            where.append(expr)
            params[column] = value
    if format_valid is not None:
        where.append("s.format_valid = %(format_valid)s")
        params["format_valid"] = format_valid
    if unread_only:
        # An attribute-only row stores the empty string rather than NULL: the
        # column has been NOT NULL since 001 and relaxing that would touch
        # every query in the platform.
        where.append("s.plate_normalised = ''")

    return _rows(
        _SELECT
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY s.ts DESC LIMIT %(limit)s",
        params,
    )


@router.get(
    "/search",
    response_model=list[Sighting],
    summary="Search sightings by plate",
    description=(
        "Exact-then-fuzzy plate lookup.\n\n"
        "The query is normalised with the same positional rules used when the "
        "sighting was written (invariant 3), so an operator typing `GJ 01 AB "
        "1234` finds a read stored as `GJO1AB1234`. If nothing matches exactly, "
        "trigram similarity is used, which is what finds a plate whose OCR lost "
        "a character.\n\n"
        "Audited as `plate.search`. Pass `X-Case-Ref` to bind the search to an "
        "investigation."
    ),
)
def search_sightings(
    plate: Annotated[str, Query(min_length=2, description="Full or partial plate.")],
    fuzzy: Annotated[bool, Query(description="Fall back to trigram similarity.")] = True,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    actor: Annotated[str, Header(alias="X-Actor")] = "anonymous",
    case_ref: Annotated[str | None, Header(alias="X-Case-Ref")] = None,
) -> list[Sighting]:
    variants = plate_key_variants(plate)
    record(
        Action.PLATE_SEARCH, actor, subject=variants[0], case_ref=case_ref,
        detail={"query": plate, "fuzzy": fuzzy},
    )

    exact = _rows(
        _SELECT + " WHERE s.plate_normalised = ANY(%(keys)s)"
        " ORDER BY s.ts DESC LIMIT %(limit)s",
        {"keys": variants, "limit": limit},
    )
    if exact or not fuzzy:
        return exact

    # Trigram, not edit distance, because the index supports it: a full scan
    # over months of sightings would miss the sub-2-second budget entirely.
    # `identifying` only: trigram similarity against a one-character read is
    # noise, and the wide-area government cameras produce a lot of it. Nothing is
    # hidden — an exact-key lookup above still finds those rows.
    return _rows(
        _SELECT + " WHERE s.identifying AND s.plate_normalised %% %(q)s"
        " ORDER BY similarity(s.plate_normalised, %(q)s) DESC, s.ts DESC"
        " LIMIT %(limit)s",
        {"q": variants[0], "limit": limit},
    )


@router.get(
    "/stats",
    summary="Sighting counts and read quality",
    description=(
        "What the pipeline has actually produced, split by scene condition.\n\n"
        "Reported per condition rather than as one number on purpose: three of "
        "the four real feeds observed are night scenes with headlight bloom, and "
        "a single headline accuracy averaged across daylight and glare is an "
        "average of two different problems."
    ),
)
def sighting_stats() -> dict[str, Any]:
    totals = fetch_one(
        "SELECT count(*) AS sightings,"
        " count(*) FILTER (WHERE format_valid) AS format_valid,"
        " count(DISTINCT plate_normalised) AS distinct_plates,"
        " count(DISTINCT camera_id) AS cameras_contributing,"
        " avg(confidence)::float AS mean_confidence,"
        " min(ts) AS earliest, max(ts) AS latest"
        " FROM sightings"
    ) or {}

    by_condition = fetch_all(
        "SELECT coalesce(condition, 'unknown') AS condition, count(*) AS sightings,"
        " count(*) FILTER (WHERE format_valid) AS format_valid,"
        " avg(confidence)::float AS mean_confidence,"
        " avg(read_count)::float AS mean_reads_per_track"
        " FROM sightings GROUP BY 1 ORDER BY 2 DESC"
    )

    return {
        "total": totals.get("sightings", 0),
        "format_valid": totals.get("format_valid", 0),
        "distinct_plates": totals.get("distinct_plates", 0),
        "cameras_contributing": totals.get("cameras_contributing", 0),
        "mean_confidence": round(totals.get("mean_confidence") or 0.0, 4),
        "earliest": totals["earliest"].isoformat() if totals.get("earliest") else None,
        "latest": totals["latest"].isoformat() if totals.get("latest") else None,
        "by_condition": [
            {
                **r,
                "mean_confidence": round(r["mean_confidence"] or 0.0, 4),
                "mean_reads_per_track": round(r["mean_reads_per_track"] or 0.0, 2),
            }
            for r in by_condition
        ],
        "caveat": (
            "Reads that fail the Indian plate format are counted here and kept in "
            "the index at reduced confidence; they are not errors to be hidden."
        ),
    }


# --- evidence images ----------------------------------------------------

#: Read-only mount of the volume the ANPR workers write crops to. The path is
#: never in the database — rows hold a relative path, so the same records work
#: whether the images sit on a host volume, a PVC or object storage later.
CROP_ROOT = pathlib.Path(os.environ.get("ANPR_CROP_ROOT", "/data/crops"))


@router.get(
    "/{sighting_id}/crop",
    summary="The evidence image for one sighting",
    description=(
        "The vehicle as it was seen, cropped from the frame whose plate read "
        "best, with the plate box drawn on it. This is what an operator judges "
        "an alert on — the plate string alone is an assertion — and it is the "
        "thumbnail the M6 detection report embeds.\n\n"
        "404 when the sighting produced no crop, or when the crop has aged out "
        "under image retention while the row itself was kept."
    ),
    response_class=FileResponse,
    responses={404: {"description": "No crop for this sighting."}},
)
def sighting_crop(sighting_id: int) -> FileResponse:
    row = fetch_one(
        "SELECT crop_path FROM sightings WHERE id = %(id)s", {"id": sighting_id}
    )
    if row is None:
        raise HTTPException(status_code=404, detail="sighting not found")
    if not row["crop_path"]:
        raise HTTPException(status_code=404, detail="this sighting has no crop")

    # Resolved and checked against the root: `crop_path` is written by the
    # pipeline, but a path from the database is still input, and `../` in it
    # would otherwise serve any file the API can read.
    target = (CROP_ROOT / row["crop_path"]).resolve()
    if not target.is_file() or CROP_ROOT.resolve() not in target.parents:
        raise HTTPException(status_code=404, detail="crop file is no longer stored")
    return FileResponse(target, media_type="image/jpeg")


# --- vehicle re-identification (M9) --------------------------------------


class SimilarVehicle(BaseModel):
    """One candidate match, with the distance that produced it."""

    id: int
    ts: str
    camera_id: str
    camera_name: str | None = None
    district: str | None = None
    lat: float | None = None
    lon: float | None = None
    plate_normalised: str
    confidence: float
    vehicle_class: str | None = None
    thumbnail_url: str | None = None
    #: Cosine distance. 0 is identical; the endpoint's default bar is 0.15.
    distance: float
    #: True when this candidate carries a *different* plate to the query's.
    #: Interesting in both directions: it is either a re-read of a plate that
    #: was misread once, or two vehicles that look alike — and occasionally it
    #: is the same plate on two different-looking vehicles, which is a clone.
    different_plate: bool


@router.get(
    "/{sighting_id}/similar",
    response_model=list[SimilarVehicle],
    summary="Find vehicles that look like this one",
    description=(
        "Appearance-based re-identification across cameras, using the "
        "`pgvector` descriptor stored with each sighting.\n\n"
        "**What this is for.** A plate read can be missing — too far, too "
        "oblique, obscured, or shed by the OCR stage under load. Appearance is "
        "a second handle on *is this the same vehicle* that does not depend on "
        "reading anything. It is also the only evidence that can contradict a "
        "plate: two sightings of one registration whose vehicles look nothing "
        "alike is what a cloned plate looks like from the other direction.\n\n"
        "**What this is not.** The descriptor is a colour and shape signature, "
        "not a learned re-identification embedding — a trained model would be "
        "markedly better at telling two silver hatchbacks apart. Results are "
        "returned as ranked candidates with their distances, to be judged by an "
        "operator against the crops. They are not an assertion of identity, and "
        "nothing in the platform treats them as one.\n\n"
        "Audited as `plate.search`, because it is a search for a vehicle."
    ),
    responses={404: {"description": "No such sighting, or it has no descriptor."}},
)
def similar_vehicles(
    sighting_id: int,
    within_hours: Annotated[int, Query(ge=1, le=168, description="Time window either side.")] = 6,
    max_distance: Annotated[float, Query(ge=0.0, le=1.0)] = 0.15,
    exclude_same_camera: Annotated[bool, Query(
        description="Movement between cameras is usually what is being looked for."
    )] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    actor: Annotated[str, Header(alias="X-Actor")] = "anonymous",
    case_ref: Annotated[str | None, Header(alias="X-Case-Ref")] = None,
) -> list[SimilarVehicle]:
    origin = fetch_one(
        "SELECT id, ts, camera_id::text AS camera_id, plate_normalised,"
        " embedding IS NOT NULL AS has_embedding"
        " FROM sightings WHERE id = %(id)s",
        {"id": sighting_id},
    )
    if origin is None:
        raise HTTPException(status_code=404, detail="sighting not found")
    if not origin["has_embedding"]:
        raise HTTPException(
            status_code=404,
            detail=(
                "this sighting has no appearance descriptor — it predates M9, or "
                "its crop could not be encoded"
            ),
        )

    record(
        Action.PLATE_SEARCH, actor, subject=origin["plate_normalised"], case_ref=case_ref,
        detail={"mode": "appearance", "sighting_id": sighting_id,
                "within_hours": within_hours, "max_distance": max_distance},
    )

    # The origin's own vector is fetched by the subquery rather than round-
    # tripped through this process: it keeps a 64-float array out of the
    # request and lets the index do the comparison where the data already is.
    rows = fetch_all(
        """
        SELECT s.id, s.ts, s.camera_id::text AS camera_id, c.name AS camera_name,
               c.district, ST_Y(c.geom::geometry) AS lat, ST_X(c.geom::geometry) AS lon,
               s.plate_normalised, s.confidence, s.vehicle_class, s.crop_path,
               (s.embedding <=> (SELECT embedding FROM sightings WHERE id = %(id)s))
                   AS distance
          FROM sightings s
          JOIN cameras c ON c.id = s.camera_id
         WHERE s.embedding IS NOT NULL
           AND s.id <> %(id)s
           AND s.ts BETWEEN %(ts)s::timestamptz - make_interval(hours => %(hours)s)
                        AND %(ts)s::timestamptz + make_interval(hours => %(hours)s)
           AND (NOT %(exclude_camera)s OR s.camera_id <> %(camera)s::uuid)
           AND (s.embedding <=> (SELECT embedding FROM sightings WHERE id = %(id)s))
               <= %(max_distance)s
         ORDER BY distance
         LIMIT %(limit)s
        """,
        {
            "id": sighting_id, "ts": origin["ts"].isoformat(), "hours": within_hours,
            "exclude_camera": exclude_same_camera, "camera": origin["camera_id"],
            "max_distance": max_distance, "limit": limit,
        },
    )

    return [
        SimilarVehicle(
            id=r["id"], ts=r["ts"].isoformat(), camera_id=r["camera_id"],
            camera_name=r["camera_name"], district=r["district"],
            lat=r["lat"], lon=r["lon"], plate_normalised=r["plate_normalised"],
            confidence=r["confidence"], vehicle_class=r["vehicle_class"],
            thumbnail_url=(
                f"/api/sightings/{r['id']}/crop" if r["crop_path"] else None
            ),
            distance=round(float(r["distance"]), 4),
            different_plate=r["plate_normalised"] != origin["plate_normalised"],
        )
        for r in rows
    ]
