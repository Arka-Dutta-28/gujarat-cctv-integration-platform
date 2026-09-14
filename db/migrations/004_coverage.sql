-- 004_coverage — camera coverage wedges and corridor gap analysis.
--
-- `bearing`, `fov_degrees` and `range_m` are not decoration: they define what a
-- camera can actually see, and therefore where the estate is blind. Computing
-- that in PostGIS rather than in application code is the point of having chosen
-- a spatial database.

-- ---------------------------------------------------------------
-- Coverage wedge
-- ---------------------------------------------------------------

-- A camera sees a circular sector: apex at the camera, opening `fov` degrees
-- either side of `bearing`, out to `range_m`. Built by walking the arc and
-- closing the ring back through the apex.
--
-- Bearings are compass degrees (clockwise from north); ST_Project also takes
-- azimuth in radians clockwise from north, so no conversion is needed beyond
-- degrees to radians.
CREATE OR REPLACE FUNCTION camera_wedge(
    origin      GEOGRAPHY(POINT, 4326),
    bearing_deg NUMERIC,
    fov_deg     NUMERIC,
    range_m     NUMERIC,
    arc_steps   INT DEFAULT 24
) RETURNS GEOGRAPHY AS $$
DECLARE
    start_deg NUMERIC := bearing_deg - fov_deg / 2.0;
    pts       GEOMETRY[];
    i         INT;
BEGIN
    IF origin IS NULL OR range_m IS NULL OR range_m <= 0 THEN
        RETURN NULL;
    END IF;

    -- A camera with no recorded bearing or field of view has not been surveyed.
    -- Returning NULL keeps it out of the covered area entirely, which is the
    -- honest answer: we do not know what it sees, so we must not claim its
    -- ground is covered.
    IF bearing_deg IS NULL OR fov_deg IS NULL THEN
        RETURN NULL;
    END IF;

    -- A full-circle field of view is a disc, not a wedge; the apex would be a
    -- degenerate spike.
    IF fov_deg >= 360 THEN
        RETURN ST_Buffer(origin, range_m);
    END IF;

    pts := ARRAY[origin::geometry];
    FOR i IN 0..arc_steps LOOP
        pts := pts || ST_Project(
            origin,
            range_m,
            radians(start_deg + (fov_deg * i / arc_steps))
        )::geometry;
    END LOOP;
    pts := pts || origin::geometry;

    RETURN ST_MakePolygon(ST_MakeLine(pts))::geography;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

COMMENT ON FUNCTION camera_wedge IS
    'Circular-sector coverage for one camera. NULL when the camera has not been '
    'surveyed (no bearing or no field of view) — an unknown view must never be '
    'reported as covered ground.';

-- ---------------------------------------------------------------
-- Corridors — the reference lines gap analysis is measured against
-- ---------------------------------------------------------------
--
-- Gaps only mean something relative to a route somebody cares about. Holding
-- corridors as data rather than as a constant in application code means an
-- operator can add "the road past the stadium" without a deploy.

CREATE TABLE corridors (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    geom        GEOGRAPHY(LINESTRING, 4326) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX corridors_geom_idx ON corridors USING GIST (geom);

-- A PTZ can point anywhere within its range, so its instantaneous wedge is only
-- where it happens to be aimed right now. This view records both readings: the
-- wedge as configured, and whether that wedge is trustworthy as a statement
-- about permanent coverage.
CREATE VIEW camera_coverage AS
SELECT
    c.id            AS camera_id,
    c.external_ref,
    c.name,
    c.kind,
    c.status,
    c.bearing,
    c.fov_degrees,
    c.range_m,
    camera_wedge(c.geom, c.bearing, c.fov_degrees, c.range_m) AS wedge,
    (c.bearing IS NULL OR c.fov_degrees IS NULL OR c.range_m IS NULL) AS unsurveyed,
    (c.kind = 'ptz')                                                  AS variable
FROM cameras c
WHERE c.status <> 'decommissioned';

COMMENT ON VIEW camera_coverage IS
    'Per-camera coverage wedge. `unsurveyed` means geometry is missing and the '
    'camera contributes nothing to covered area; `variable` means the camera is '
    'a PTZ, so its wedge describes the current preset rather than the camera.';
