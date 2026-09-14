"""Camera registry — the control plane (Model 1).

Everything geographic here is GeoJSON. The map layer consumes the
FeatureCollection directly, and so would QGIS or any other GIS tool an evaluator
points at the API.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from services.api.auth import Principal, current_principal, require_operator
from services.common.audit import Action, record
from services.common.db import fetch_all, fetch_one

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


def actor(principal: Annotated[Principal, Depends(current_principal)]) -> str:
    """Who is acting, for the audit trail.

    This is the function M1 said M8 would replace, and replacing it was the
    whole point of routing every call site through one dependency: the identity
    now comes from a signed token instead of a header, and no endpoint changed.

    With `AUTH_REQUIRED=false` — local development and the acceptance tests —
    `current_principal` still honours `X-Actor`, so existing behaviour and every
    audit entry already written mean exactly what they meant before.
    """
    return principal.username


def writer(
    principal: Annotated[Principal, Depends(require_operator)],
) -> str:
    """Who is acting, where the action changes something.

    Separate from `actor` so read-only endpoints stay open to a `viewer`. This
    is what makes the submission's demo account genuinely read-only rather than
    read-only by convention.
    """
    return principal.username


Actor = Annotated[str, Depends(actor)]
Writer = Annotated[str, Depends(writer)]


class Camera(BaseModel):
    id: str
    external_ref: str | None
    name: str
    department: str | None
    ownership_type: str
    adapter: str
    stream_ref: str
    lat: float
    lon: float
    address: str | None
    district: str | None
    bearing: int | None
    fov_degrees: int | None
    range_m: int | None
    status: str
    last_seen: str | None
    retention_days: int | None
    #: Codec, container, resolution and declared frame rate as the upstream
    #: catalogue reported them. Exposed because an integrator sizing decoders
    #: for this estate needs to know it is not uniform — and because
    #: `declared_fps` is worth showing next to the measured rate on the
    #: performance page, which is the evidence that the two disagree.
    stream_properties: dict[str, Any] = Field(default_factory=dict)
    #: Every transport this camera publishes. A client that can only reach HLS
    #: should not have to guess a URL to find out.
    endpoints: list[dict[str, Any]] = Field(default_factory=list)
    #: How the position was obtained — `landmark`, `city`, `district`,
    #: `unplaced` or `survey`. Coverage and gap analysis must not treat a
    #: district centroid as a surveyed position, and an operator needs to see
    #: which pins still need one.
    geo_precision: str | None = None


class Feature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: dict[str, Any]
    properties: dict[str, Any]


class FeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[Feature]
    # Not part of the GeoJSON spec, but harmless to consumers and saves the UI a
    # second round trip just to render a count.
    count: int = Field(description="Number of features, for convenience.")


# One projection of the registry, shared by both representations below, so the
# GeoJSON and JSON views can never drift apart.
_SELECT = """
    SELECT c.id::text,
           c.external_ref,
           c.name,
           d.name              AS department,
           c.ownership_type::text,
           c.adapter::text,
           c.stream_ref,
           ST_Y(c.geom::geometry) AS lat,
           ST_X(c.geom::geometry) AS lon,
           c.address,
           c.district,
           c.bearing,
           c.fov_degrees,
           c.range_m,
           c.status::text,
           c.last_seen,
           c.retention_days,
           COALESCE(c.stream_properties, '{}'::jsonb) AS stream_properties,
           COALESCE(c.endpoints, '[]'::jsonb)         AS endpoints,
           c.geo_precision
      FROM cameras c
      LEFT JOIN departments d ON d.id = c.department_id
"""


def _where(
    status: str | None, district: str | None, department: str | None
) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {}
    if status:
        # Asking for a status by name means you want exactly that status,
        # retired cameras included — otherwise there is no way to find one to
        # bring back into service. Every other listing hides them.
        clauses = ["c.status::text = %(status)s"]
        params["status"] = status
    else:
        clauses = ["c.status <> 'decommissioned'"]
    if district:
        clauses.append("c.district = %(district)s")
        params["district"] = district
    if department:
        clauses.append("d.name = %(department)s")
        params["department"] = department
    return " WHERE " + " AND ".join(clauses), params


def _to_feature(row: dict[str, Any]) -> Feature:
    props = {k: v for k, v in row.items() if k not in {"lat", "lon"}}
    if props.get("last_seen") is not None:
        props["last_seen"] = props["last_seen"].isoformat()
    return Feature(
        # GeoJSON positions are [lon, lat]. Getting this backwards puts every
        # Gujarat camera in the Indian Ocean, so it is centralised here.
        geometry={"type": "Point", "coordinates": [row["lon"], row["lat"]]},
        properties=props,
    )


@router.get(
    "/geojson",
    response_model=FeatureCollection,
    summary="Camera registry as GeoJSON",
    description=(
        "Every non-decommissioned camera as a GeoJSON FeatureCollection, one "
        "Point per camera. This is what the map layer renders. Bearing, field "
        "of view and range travel in `properties` so the client can draw "
        "coverage wedges without a second call."
    ),
)
def cameras_geojson(
    status: str | None = Query(None, description="Filter by camera status."),
    district: str | None = Query(None, description="Filter by district."),
    department: str | None = Query(None, description="Filter by owning department."),
) -> FeatureCollection:
    where, params = _where(status, district, department)
    rows = fetch_all(_SELECT + where + " ORDER BY c.external_ref", params)
    features = [_to_feature(r) for r in rows]
    return FeatureCollection(features=features, count=len(features))


@router.get(
    "",
    response_model=list[Camera],
    summary="List cameras",
    description="The registry as plain JSON, for clients that are not drawing a map.",
)
def list_cameras(
    status: str | None = Query(None, description="Filter by camera status."),
    district: str | None = Query(None, description="Filter by district."),
    department: str | None = Query(None, description="Filter by owning department."),
) -> list[Camera]:
    where, params = _where(status, district, department)
    rows = fetch_all(_SELECT + where + " ORDER BY c.external_ref", params)
    return [
        Camera(**{**r, "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None})
        for r in rows
    ]


# --- ANPR capability -----------------------------------------------------
#
# Declared before `/{camera_id}` because FastAPI matches routes in declaration
# order and `anpr-capability` would otherwise be read as a camera UUID.


class CameraCapability(BaseModel):
    camera_id: str
    name: str | None = None
    external_ref: str | None = None
    district: str | None = None
    status: str | None = None
    grade: str = Field(
        description=(
            "anpr_grade | marginal | situational_awareness | "
            "insufficient_evidence | no_reads"
        )
    )
    reason: str = Field(description="The measurement behind the grade, in words.")
    counts_toward_accuracy: bool
    sightings: int
    median_plate_px: float | None = None
    mean_chars: float
    identifying_fraction: float
    format_valid_fraction: float
    wide_enough_fraction: float


_CAPABILITY_SQL = """
WITH reads AS (
    SELECT s.camera_id,
           char_length(s.plate_normalised) AS chars,
           s.identifying,
           s.format_valid,
           CASE WHEN s.plate_bbox IS NOT NULL
                THEN (s.plate_bbox[3] - s.plate_bbox[1])::float
           END AS plate_px
      FROM sightings s
     WHERE s.ts > now() - make_interval(days => %(days)s)
)
SELECT c.id::text AS camera_id, c.name, c.external_ref, c.district, c.status,
       count(r.chars)::int AS sightings,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY r.plate_px) AS median_plate_px,
       coalesce(avg(r.chars), 0)::float AS mean_chars,
       CASE WHEN count(r.chars) > 0
            THEN count(*) FILTER (WHERE r.identifying)::float / count(r.chars)
            ELSE 0 END AS identifying_fraction,
       CASE WHEN count(r.chars) > 0
            THEN count(*) FILTER (WHERE r.format_valid)::float / count(r.chars)
            ELSE 0 END AS format_valid_fraction,
       CASE WHEN count(r.plate_px) > 0
            THEN count(*) FILTER (WHERE r.plate_px >= %(floor_px)s)::float
                 / count(r.plate_px)
            ELSE 0 END AS wide_enough_fraction
  FROM cameras c
  LEFT JOIN reads r ON r.camera_id = c.id
 WHERE c.status <> 'decommissioned'
 GROUP BY 1, 2, 3, 4, 5
 ORDER BY median_plate_px DESC NULLS LAST, c.external_ref
"""


@router.get(
    "/anpr-capability",
    summary="Which cameras can actually read a plate",
    description=(
        "Per-camera ANPR capability, derived from plate crops the pipeline "
        "already measured — not from a configuration field.\n\n"
        "This exists because of a finding, not a feature request. The 31 "
        "government feeds produced hundreds of reads and **zero** structurally "
        "valid plates: their plate crops average 66 px across against the "
        "simulated farm's 276 px, which is about 7 px per character. They are "
        "wide-area situational-awareness views, correctly deployed and unable to "
        "resolve a registration number.\n\n"
        "Consequently an accuracy figure must be **scoped**. "
        "`counts_toward_accuracy` marks the cameras any headline ANPR number is "
        "true of; `summary.anpr_grade` is its denominator. Quoting an "
        "estate-wide figure would average two different problems.\n\n"
        "Nothing here filters or hides a read (invariant 1) — every read remains "
        "persisted and findable. This grades the *source*."
    ),
)
def anpr_capability(
    days: Annotated[int, Query(ge=1, le=90, description="Window of reads to judge on.")] = 7,
) -> dict[str, Any]:
    from services.anpr import capability as cap

    rows = fetch_all(_CAPABILITY_SQL, {"days": days, "floor_px": cap.MIN_ANPR_PLATE_PX})

    graded: list[CameraCapability] = []
    assessments: list[cap.Assessment] = []
    for r in rows:
        evidence = cap.CameraEvidence(
            camera_id=r["camera_id"], name=r["name"], external_ref=r["external_ref"],
            district=r["district"], sightings=r["sightings"],
            median_plate_px=r["median_plate_px"], mean_chars=r["mean_chars"],
            identifying_fraction=r["identifying_fraction"],
            format_valid_fraction=r["format_valid_fraction"],
            wide_enough_fraction=r["wide_enough_fraction"],
        )
        verdict = cap.assess(evidence)
        assessments.append(verdict)
        graded.append(
            CameraCapability(
                camera_id=evidence.camera_id, name=evidence.name,
                external_ref=evidence.external_ref, district=evidence.district,
                status=r["status"], grade=verdict.grade, reason=verdict.reason,
                counts_toward_accuracy=verdict.counts_toward_accuracy,
                sightings=evidence.sightings,
                median_plate_px=(
                    round(evidence.median_plate_px, 1)
                    if evidence.median_plate_px is not None else None
                ),
                mean_chars=round(evidence.mean_chars, 2),
                identifying_fraction=round(evidence.identifying_fraction, 4),
                format_valid_fraction=round(evidence.format_valid_fraction, 4),
                wide_enough_fraction=round(evidence.wide_enough_fraction, 4),
            )
        )

    summary = cap.summarise(assessments)
    capable = [g for g in graded if g.counts_toward_accuracy]
    return {
        "window_days": days,
        "cameras_assessed": len(graded),
        "summary": summary,
        "anpr_plate_px_floor": cap.MIN_ANPR_PLATE_PX,
        "min_samples": cap.MIN_SAMPLES,
        "accuracy_scope": (
            f"{len(capable)} of {len(graded)} cameras are ANPR-grade. Any plate-"
            f"accuracy figure applies to those and to no others."
        ),
        "cameras": graded,
    }


@router.get(
    "/{camera_id}",
    response_model=Camera,
    summary="Get one camera",
    description="Full registry record for a single camera, by UUID.",
    responses={404: {"description": "No such camera."}},
)
def get_camera(camera_id: str) -> Camera:
    from fastapi import HTTPException

    row = fetch_one(_SELECT + " WHERE c.id = %(id)s", {"id": camera_id})
    if row is None:
        raise HTTPException(status_code=404, detail="camera not found")
    return Camera(
        **{**row, "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None}
    )


# --- write models ------------------------------------------------------


class CameraCreate(BaseModel):
    """Everything needed to onboard a camera.

    Deliberately flat and small: onboarding must be demonstrable in about
    thirty seconds, which rules out a nested payload assembled by a wizard.
    Only name, adapter, stream_ref and a position are required — a camera whose
    survey data has not arrived yet is still worth having in the registry, and
    the real government feeds arrive exactly that way.
    """

    name: str = Field(min_length=1, max_length=200, examples=["Chimanbhai Bridge PTZ"])
    adapter: Literal["rtsp", "file", "hls", "onvif", "vendor_sdk", "vms_api", "http"]
    stream_ref: str = Field(min_length=1, examples=["rtsp://10.0.0.5:554/stream1"])
    lat: float = Field(ge=-90, le=90, examples=[23.0301])
    lon: float = Field(ge=-180, le=180, examples=[72.5100])

    external_ref: str | None = Field(None, max_length=120)
    department: str | None = Field(None, description="Department name, must already exist.")
    ownership_type: Literal["government", "private_public_facing"] = "government"
    kind: Literal["fixed", "ptz", "unknown"] = "unknown"
    address: str | None = None
    district: str | None = None
    bearing: int | None = Field(None, ge=0, le=359)
    fov_degrees: int | None = Field(None, ge=1, le=360)
    range_m: int | None = Field(None, ge=1, le=2000)
    mounting_height_m: float | None = Field(None, ge=0, le=99)
    retention_days: int | None = Field(None, ge=1, le=3650)
    credential_ref: str | None = Field(
        None,
        description="Vault key. Never a secret — invariant 5.",
    )

    @field_validator("stream_ref")
    @classmethod
    def _no_inline_credentials(cls, v: str) -> str:
        """Reject credentials embedded in the URL.

        `rtsp://user:pass@host/path` would put a live password into the registry,
        into every API response and into the audit log. Credentials belong behind
        `credential_ref` (invariant 5), so this is refused at the door rather
        than sanitised later.
        """
        head = v.split("://", 1)[-1].split("/", 1)[0]
        if "@" in head and ":" in head.split("@", 1)[0]:
            raise ValueError(
                "stream_ref must not embed credentials; put them in the vault "
                "and reference them with credential_ref"
            )
        return v


class CameraUpdate(BaseModel):
    """Partial update. Every field optional; unset fields are left alone."""

    name: str | None = Field(None, min_length=1, max_length=200)
    adapter: Literal["rtsp", "file", "hls", "onvif", "vendor_sdk", "vms_api", "http"] | None = None
    stream_ref: str | None = None
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)
    department: str | None = None
    ownership_type: Literal["government", "private_public_facing"] | None = None
    kind: Literal["fixed", "ptz", "unknown"] | None = None
    address: str | None = None
    district: str | None = None
    bearing: int | None = Field(None, ge=0, le=359)
    fov_degrees: int | None = Field(None, ge=1, le=360)
    range_m: int | None = Field(None, ge=1, le=2000)
    mounting_height_m: float | None = Field(None, ge=0, le=99)
    retention_days: int | None = Field(None, ge=1, le=3650)
    credential_ref: str | None = None

    _no_inline_credentials = field_validator("stream_ref")(
        CameraCreate._no_inline_credentials.__func__  # type: ignore[attr-defined]
    )


def _department_id(name: str | None) -> int | None:
    """Resolve a department by display name *or* code, case-insensitively.

    It used to match the display name only, so `Police` worked and `POLICE`
    was rejected as unknown — while the code column says `POLICE` and that is
    what the registry, the onboarding sync and every log line show. An operator
    reading a camera's department off the map and typing it back into the form
    got "unknown department" for a department that plainly exists.

    Both are unambiguous — no code collides with another department's name — so
    accepting either costs nothing and removes a snag from a flow that is
    demonstrated live and timed.
    """
    if not name:
        return None
    row = fetch_one(
        "SELECT id FROM departments WHERE lower(name) = lower(%(v)s)"
        "    OR lower(code) = lower(%(v)s)",
        {"v": name.strip()},
    )
    if row is None:
        known = fetch_all("SELECT code, name FROM departments ORDER BY code")
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown department: {name!r}. Known: "
                + ", ".join(f"{d['code']} ({d['name']})" for d in known)
            ),
        )
    return row["id"]


def _get_or_404(camera_id: str) -> dict[str, Any]:
    row = fetch_one(_SELECT + " WHERE c.id = %(id)s", {"id": camera_id})
    if row is None:
        raise HTTPException(status_code=404, detail="camera not found")
    return row


@router.post(
    "",
    response_model=Camera,
    status_code=201,
    summary="Onboard a camera",
    description=(
        "Adds a camera to the registry. This is the only way a camera enters "
        "the platform — no component holds a hardcoded stream URL. The new "
        "camera starts `unknown` and the health prober moves it to `online` "
        "within a few seconds once its stream answers.\n\n"
        "`stream_ref` must not embed credentials; use `credential_ref`."
    ),
    responses={
        409: {"description": "A camera with this external_ref is already registered."},
        422: {"description": "Unknown department, or credentials in stream_ref."},
    },
)
def create_camera(who: Writer, payload: Annotated[CameraCreate, Body()]) -> Camera:
    dept_id = _department_id(payload.department)
    try:
        row = _insert_camera(payload, dept_id)
    except psycopg.errors.UniqueViolation as exc:
        # A re-onboarded camera is an ordinary thing for an operator to do —
        # re-running a bulk import, or adding a camera a colleague already
        # added — and it was answering **500 Internal Server Error**, which
        # reads as a broken platform rather than as "you already have this one".
        # Onboarding is demonstrated live and graded, so the failure a person is
        # most likely to hit must say what happened and what to do about it.
        raise HTTPException(
            status_code=409,
            detail=(
                f"a camera with external_ref {payload.external_ref!r} is already "
                f"registered. Use PATCH to update it, or choose a different ref."
            ),
        ) from exc

    record(
        Action.CAMERA_CREATE, who, subject=str(row["id"]),
        detail={"name": payload.name, "adapter": payload.adapter},
    )
    return get_camera(str(row["id"]))


def _insert_camera(payload: CameraCreate, dept_id: str) -> dict[str, Any]:
    row = fetch_one(
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
        RETURNING id
        """,
        {**payload.model_dump(), "department_id": dept_id},
    )
    assert row is not None
    return row


@router.patch(
    "/{camera_id}",
    response_model=Camera,
    summary="Update a camera",
    description=(
        "Partial update; unset fields are left untouched. Swapping a simulated "
        "feed for a real one is a `stream_ref` change here and nothing else."
    ),
    responses={404: {"description": "No such camera."}},
)
def update_camera(
    camera_id: str, who: Writer, payload: Annotated[CameraUpdate, Body()]
) -> Camera:
    before = _get_or_404(camera_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return get_camera(camera_id)

    sets: list[str] = []
    params: dict[str, Any] = {"id": camera_id}

    if "department" in changes:
        params["department_id"] = _department_id(changes.pop("department"))
        sets.append("department_id = %(department_id)s")

    # Position is one column but two inputs, so fill either from the change or
    # from the existing row rather than requiring both.
    if "lat" in changes or "lon" in changes:
        params["lat"] = changes.pop("lat", before["lat"])
        params["lon"] = changes.pop("lon", before["lon"])
        sets.append("geom = ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography")

    casts = {"adapter": "::adapter_type", "ownership_type": "::ownership", "kind": "::camera_kind"}
    for field, value in changes.items():
        params[field] = value
        sets.append(f"{field} = %({field})s{casts.get(field, '')}")

    fetch_one(f"UPDATE cameras SET {', '.join(sets)} WHERE id = %(id)s RETURNING id", params)
    record(
        Action.CAMERA_UPDATE, who, subject=camera_id,
        detail={"changed": sorted(payload.model_dump(exclude_unset=True).keys())},
    )
    return get_camera(camera_id)


@router.delete(
    "/{camera_id}",
    response_model=Camera,
    summary="Decommission a camera",
    description=(
        "Marks the camera `decommissioned` and removes it from the map and from "
        "every default listing.\n\n"
        "It is **not** deleted. `sightings` holds a foreign key to `cameras`, so "
        "a hard delete would either fail or destroy the evidence trail attached "
        "to a camera that has been recording for months. Retiring a camera must "
        "never erase what it saw."
    ),
    responses={404: {"description": "No such camera."}},
)
def decommission_camera(camera_id: str, who: Writer) -> Camera:
    _get_or_404(camera_id)
    fetch_one(
        "UPDATE cameras SET status = 'decommissioned'::camera_status"
        " WHERE id = %(id)s RETURNING id",
        {"id": camera_id},
    )
    record(Action.CAMERA_DECOMMISSION, who, subject=camera_id)
    row = fetch_one(_SELECT + " WHERE c.id = %(id)s", {"id": camera_id})
    assert row is not None
    return Camera(
        **{**row, "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None}
    )


@router.post(
    "/{camera_id}/recommission",
    response_model=Camera,
    summary="Return a decommissioned camera to service",
    description=(
        "Clears the `decommissioned` status and puts the camera back on the map "
        "with its history intact — same id, same sightings, same audit trail.\n\n"
        "A camera pulled for maintenance and refitted is the same asset; forcing "
        "an operator to re-onboard it would fork its history in two and break "
        "every journey that crosses the boundary. Status returns to `unknown`; "
        "the health prober decides within seconds whether it is really back."
    ),
    responses={
        404: {"description": "No such camera."},
        409: {"description": "Camera is not decommissioned."},
    },
)
def recommission_camera(camera_id: str, who: Writer) -> Camera:
    row = fetch_one("SELECT status::text AS status FROM cameras WHERE id = %(id)s",
                    {"id": camera_id})
    if row is None:
        raise HTTPException(status_code=404, detail="camera not found")
    if row["status"] != "decommissioned":
        raise HTTPException(
            status_code=409, detail=f"camera is {row['status']}, not decommissioned"
        )

    fetch_one(
        "UPDATE cameras SET status = 'unknown'::camera_status"
        " WHERE id = %(id)s RETURNING id",
        {"id": camera_id},
    )
    record(Action.CAMERA_RECOMMISSION, who, subject=camera_id)
    return get_camera(camera_id)
