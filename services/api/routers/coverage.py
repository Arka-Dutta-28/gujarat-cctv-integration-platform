"""Coverage and gap analysis.

bearing, fov_degrees and range_m define what a camera can actually see, and
therefore where the estate is blind. Two rules govern everything here, and both
are about not overstating what we know.

A camera with no surveyed geometry contributes no coverage. Assuming a default
wedge would paint covered ground the platform cannot actually see. 31 of the
real government cameras are in exactly this state.

A PTZ's wedge is where it is pointed now, not what it permanently covers. It is
reported separately, so an operator is never shown a moving camera's current aim
as though it were fixed coverage.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from services.common.db import fetch_all, fetch_one

router = APIRouter(prefix="/api/coverage", tags=["coverage"])


class CoverageSummary(BaseModel):
    cameras_total: int
    cameras_surveyed: int = Field(description="Cameras with bearing, FOV and range recorded.")
    cameras_unsurveyed: int = Field(description="No geometry; contribute no coverage.")
    cameras_ptz: int = Field(description="Coverage varies with the current preset.")
    covered_area_km2: float


class GapSegment(BaseModel):
    index: int
    length_m: float
    start: list[float] = Field(description="GeoJSON position [lon, lat].")
    end: list[float]


class GapAnalysis(BaseModel):
    corridor: str
    corridor_length_m: float
    covered_length_m: float
    uncovered_length_m: float
    coverage_ratio: float = Field(description="0-1 share of the corridor inside some wedge.")
    gap_count: int
    largest_gap_m: float
    gaps: list[GapSegment]
    geojson: dict[str, Any] = Field(description="Uncovered segments as a FeatureCollection.")
    caveat: str


@router.get(
    "/summary",
    response_model=CoverageSummary,
    summary="Coverage summary",
    description=(
        "Totals for the estate. `cameras_unsurveyed` is the number with no "
        "recorded bearing, field of view or range — they contribute nothing to "
        "`covered_area_km2`, because claiming coverage we cannot demonstrate "
        "would be worse than reporting a smaller number honestly."
    ),
)
def coverage_summary() -> CoverageSummary:
    row = fetch_one(
        """
        SELECT count(*)                                   AS cameras_total,
               count(*) FILTER (WHERE NOT unsurveyed)     AS cameras_surveyed,
               count(*) FILTER (WHERE unsurveyed)         AS cameras_unsurveyed,
               count(*) FILTER (WHERE variable)           AS cameras_ptz,
               COALESCE(ST_Area(ST_Union(wedge::geometry)::geography), 0) / 1e6
                                                          AS covered_area_km2
          FROM camera_coverage
        """
    )
    assert row is not None
    return CoverageSummary(**{k: (v or 0) for k, v in row.items()})


@router.get(
    "/geojson",
    summary="Coverage wedges as GeoJSON",
    description=(
        "One Polygon per surveyed camera, as a circular sector oriented on the "
        "camera's bearing. Unsurveyed cameras are omitted — they have no "
        "geometry to draw. PTZ wedges carry `variable: true`, since they show "
        "the current preset rather than permanent coverage."
    ),
)
def coverage_geojson(
    kind: Literal["all", "fixed", "ptz"] = Query("all", description="Filter by camera kind."),
) -> dict[str, Any]:
    where = "WHERE wedge IS NOT NULL"
    if kind == "ptz":
        where += " AND variable"
    elif kind == "fixed":
        where += " AND NOT variable"

    rows = fetch_all(
        f"""
        SELECT camera_id::text, external_ref, name, kind::text, status::text,
               bearing, fov_degrees, range_m, variable,
               ST_AsGeoJSON(wedge::geometry) AS geom
          FROM camera_coverage
          {where}
          ORDER BY external_ref
        """
    )
    features = [
        {
            "type": "Feature",
            "geometry": json.loads(r.pop("geom")),
            "properties": r,
        }
        for r in rows
    ]
    return {"type": "FeatureCollection", "features": features, "count": len(features)}


@router.get(
    "/gaps",
    response_model=GapAnalysis,
    summary="Corridor gap analysis",
    description=(
        "Subtracts every camera's coverage wedge from a corridor and returns "
        "what is left: the stretches along that route where a vehicle passes "
        "unseen. Gaps are returned longest-first, because the longest blind "
        "stretch is the one worth spending a camera on.\\n\\n"
        "Unsurveyed cameras are excluded, so the reported coverage is a floor "
        "rather than an estimate."
    ),
    responses={404: {"description": "No such corridor."}},
)
def coverage_gaps(
    corridor: str = Query("NH-48 Ahmedabad–Surat", description="Corridor name."),
    min_gap_m: float = Query(
        100.0, ge=0, description="Ignore gaps shorter than this, to suppress slivers."
    ),
) -> GapAnalysis:
    row = fetch_one(
        """
        WITH target AS (
            SELECT id, name, geom FROM corridors WHERE name = %(name)s
        ),
        cover AS (
            SELECT ST_Union(wedge::geometry) AS g
              FROM camera_coverage
             WHERE wedge IS NOT NULL
        ),
        cut AS (
            SELECT t.name,
                   t.geom::geometry AS line,
                   CASE
                       WHEN c.g IS NULL THEN t.geom::geometry
                       ELSE ST_Difference(t.geom::geometry, c.g)
                   END AS uncovered
              FROM target t CROSS JOIN cover c
        )
        SELECT name,
               ST_Length(line::geography)      AS corridor_length_m,
               ST_Length(uncovered::geography) AS uncovered_length_m,
               ST_AsGeoJSON(uncovered)         AS uncovered_geojson
          FROM cut
        """,
        {"name": corridor},
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown corridor: {corridor!r}")

    total = float(row["corridor_length_m"] or 0)
    uncovered_total = float(row["uncovered_length_m"] or 0)
    geom = json.loads(row["uncovered_geojson"]) if row["uncovered_geojson"] else None

    # A MultiLineString of leftovers is one blob; split it so each blind stretch
    # can be listed, measured and ranked.
    parts: list[list[list[float]]] = []
    if geom:
        if geom["type"] == "LineString":
            parts = [geom["coordinates"]]
        elif geom["type"] == "MultiLineString":
            parts = geom["coordinates"]

    segments: list[GapSegment] = []
    for coords in parts:
        if len(coords) < 2:
            continue
        length = fetch_one(
            "SELECT ST_Length(ST_GeomFromGeoJSON(%s)::geography) AS m",
            (json.dumps({"type": "LineString", "coordinates": coords}),),
        )
        metres = float((length or {}).get("m") or 0)
        if metres < min_gap_m:
            continue
        segments.append(
            GapSegment(index=0, length_m=round(metres, 1), start=coords[0], end=coords[-1])
        )

    segments.sort(key=lambda s: s.length_m, reverse=True)
    for i, seg in enumerate(segments, start=1):
        seg.index = i

    covered = max(0.0, total - uncovered_total)
    return GapAnalysis(
        corridor=row["name"],
        corridor_length_m=round(total, 1),
        covered_length_m=round(covered, 1),
        uncovered_length_m=round(uncovered_total, 1),
        coverage_ratio=round(covered / total, 4) if total else 0.0,
        gap_count=len(segments),
        largest_gap_m=segments[0].length_m if segments else 0.0,
        gaps=segments,
        geojson={
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "LineString", "coordinates": [s.start, s.end]},
                    "properties": {"index": s.index, "length_m": s.length_m},
                }
                for s in segments
            ],
        },
        caveat=(
            "Cameras without surveyed bearing, field of view or range are "
            "excluded, so covered length is a floor. PTZ wedges reflect their "
            "current preset."
        ),
    )
