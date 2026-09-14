"""Sync the government evaluation feeds into the registry from the catalogue.

One command, run as often as you like, that makes the registry agree with
whatever /api/ingest currently says. Not a one-shot seeder: the integration
reference states that camera ids and the set of available cameras can change, so
onboarding has to be a reconciliation rather than an insert.

What it does, and why each part is the way it is.

Enumerates from the catalogue, never by counting. The previous version walked
/api/cameras/1..31/state, which hardcodes both a URL pattern and an estate size,
and fails silently: a renumbered grid onboards the wrong cameras and reports
success. Everything here comes from one catalogue read.

Stores every endpoint the catalogue offers. RTSP for inference, HLS for a
restricted network, WHEP for the browser. The capture layer walks that ladder
and nothing anywhere reconstructs a URL from a template.

Stores the per-camera stream properties: codec, container, resolution, declared
rate. The reference is explicit that a fixed-shape inference batch across a
non-uniform grid will not work, and the pipeline can size itself per camera only
if the registry knows.

Positions by place name, through the gazetteer. The old table was keyed by
camera number, which is exactly the key the reference says can change. See
services/common/gazetteer.py.

Never deletes. A camera that has left the catalogue is marked offline and keeps
its row, because sightings references it and those rows are evidence.

Usage:
    SENTINEL_BASE=https://live.sentinelgujarat.in python -m scripts.seed_real_feeds
    python -m scripts.seed_real_feeds --dry-run
    python -m scripts.seed_real_feeds --catalogue-file saved.json   # offline

Exit codes, because the three failures need three different responses:

    0  the registry now agrees with the catalogue
    1  the catalogue was unreachable or unparseable: wait, or fix the parser
    2  the catalogue requires authentication: go and get credentials, since
       nothing here can work around it

In every non-zero case the registry is left exactly as it was. A sync that
half-applied would be worse than one that did not run.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
from dataclasses import asdict

import psycopg

from services.adapters.catalogue import (
    CatalogueAuthRequired,
    CatalogueCamera,
    fetch_catalogue,
    parse_catalogue,
)
from services.common.config import settings
from services.common.gazetteer import resolve

log = logging.getLogger("seed-real")

BASE = os.environ.get("SENTINEL_BASE", "https://live.sentinelgujarat.in")

#: Prefix for the platform's own reference for these cameras. Namespaced so the
#: real estate cannot collide with the simulated NH-48 farm, which stays in
#: place for dense-tracing tests.
REF_PREFIX = os.environ.get("SENTINEL_REF_PREFIX", "sentinel")

#: Which login the grid's cameras use, as a *reference*, never the secret. The
#: username and password come from `CAMERA_CRED_<REF>_USER` / `_PASSWORD` at
#: connect time (`services/adapters/credentials.py`). Added 14 Sep 2026: RTSP
#: answered 401 without a login and served 1920x1080 H.264 with the registered
#: email and access code. Unset means "no login", and never clears one an
#: operator set by hand.
CREDENTIAL_REF = os.environ.get("SENTINEL_CREDENTIAL_REF", "").strip() or None

#: Department a camera is attributed to when the catalogue does not say. The
#: evaluation grid is police-provided; anything the catalogue *does* label is
#: used instead.
DEFAULT_DEPARTMENT = os.environ.get("SENTINEL_DEFAULT_DEPARTMENT", "POLICE")

#: Words in a device label that mean the unit moves. A PTZ's bearing describes
#: its current preset rather than the camera, and coverage analysis has to know.
_PTZ_HINTS = ("ptz", "dome", "speed dome", "pan tilt", "pantilt")
_FIXED_HINTS = ("fix", "fixed", "bullet", "static")


def _kind_of(camera: CatalogueCamera) -> str:
    """Fixed or PTZ, from whatever the catalogue calls the device.

    Inferred from the device label rather than looked up per camera: the label
    is what actually carries the information (`CSITMS-32_PTZ2`, `FIX-3`), and a
    per-camera table would be one more thing keyed by an id that can change.
    """
    declared = (camera.kind or "").strip().lower()
    if declared in {"fixed", "ptz"}:
        return declared
    haystack = " ".join(
        part.lower() for part in (camera.name, camera.location, camera.kind) if part
    )
    if any(hint in haystack for hint in _PTZ_HINTS):
        return "ptz"
    if any(hint in haystack for hint in _FIXED_HINTS):
        return "fixed"
    return "unknown"


def _row_for(camera: CatalogueCamera) -> dict:
    """One registry row from one catalogue entry."""
    primary = camera.primary
    location = camera.location or camera.name

    # A catalogue that gives coordinates is believed; the gazetteer is only for
    # the case this one has — a location name and nothing else.
    if camera.lat is not None and camera.lon is not None:
        lat, lon = camera.lat, camera.lon
        district = camera.district or resolve(location, hint=camera.name).district
        precision = "survey"
        matched = None
    else:
        place = resolve(location, hint=camera.name)
        lat, lon, precision, matched = place.lat, place.lon, place.precision, place.matched
        district = camera.district or place.district

    return {
        "external_ref": f"{REF_PREFIX}-{camera.source_id}",
        "source_id": camera.source_id,
        "name": camera.name,
        "department_code": (camera.department or DEFAULT_DEPARTMENT).upper(),
        "adapter": camera.adapter,
        # The endpoint a decoder should prefer, as the catalogue gave it. The
        # full ladder goes in `endpoints` beside it.
        "stream_ref": primary.url if primary else "",
        "credential_ref": CREDENTIAL_REF,
        "lat": lat,
        "lon": lon,
        "district": district,
        "address": location,
        "kind": _kind_of(camera),
        "geo_precision": precision,
        "geo_matched": matched,
        # Unknown for a real camera until surveyed. Left null rather than
        # invented: bearing and FOV drive coverage polygons, and made-up values
        # produce confidently wrong coverage.
        "bearing": None,
        "fov_degrees": None,
        "range_m": None,
        # Three states, not two. `live: false` from the catalogue is a real
        # claim that the camera is down and should be shown as such; a camera
        # the catalogue says nothing about is `unknown`, which is a different
        # thing and is what the health prober then resolves. Collapsing them
        # would either hide known-down cameras or invent a fault for silent ones.
        "status": {True: "online", False: "offline"}.get(camera.live, "unknown"),
        "endpoints": json.dumps([asdict(e) for e in camera.endpoints]),
        "stream_properties": json.dumps(
            {
                k: v
                for k, v in {
                    "codec": camera.codec,
                    "container": camera.container,
                    "width": camera.width,
                    "height": camera.height,
                    "declared_fps": camera.declared_fps,
                    "bitrate_kbps": camera.bitrate_kbps,
                    "live": camera.live,
                }.items()
                if v is not None
            }
        ),
    }


_UPSERT = """
INSERT INTO cameras (
    external_ref, source_id, name, department_id, ownership_type,
    adapter, stream_ref, credential_ref, geom, address, district, kind,
    bearing, fov_degrees, range_m, status, retention_days,
    stream_properties, endpoints, geo_precision, catalogue_seen_at
) VALUES (
    %(external_ref)s, %(source_id)s, %(name)s, %(department_id)s,
    'government'::ownership,
    %(adapter)s::adapter_type, %(stream_ref)s, %(credential_ref)s,
    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
    %(address)s, %(district)s, %(kind)s::camera_kind,
    %(bearing)s, %(fov_degrees)s, %(range_m)s,
    %(status)s::camera_status, 30,
    %(stream_properties)s::jsonb, %(endpoints)s::jsonb, %(geo_precision)s, now()
)
ON CONFLICT (external_ref) DO UPDATE SET
    source_id         = EXCLUDED.source_id,
    name              = EXCLUDED.name,
    department_id     = EXCLUDED.department_id,
    adapter           = EXCLUDED.adapter,
    stream_ref        = EXCLUDED.stream_ref,
    credential_ref    = COALESCE(EXCLUDED.credential_ref, cameras.credential_ref),
    address           = EXCLUDED.address,
    district          = EXCLUDED.district,
    kind              = EXCLUDED.kind,
    stream_properties = EXCLUDED.stream_properties,
    endpoints         = EXCLUDED.endpoints,
    catalogue_seen_at = now(),
    -- A surveyed position, once entered by an operator, outranks anything this
    -- script can derive from a place name. Only overwrite a position we
    -- ourselves guessed.
    geom = CASE WHEN cameras.geo_precision = 'survey'
                THEN cameras.geom ELSE EXCLUDED.geom END,
    geo_precision = CASE WHEN cameras.geo_precision = 'survey'
                         THEN cameras.geo_precision ELSE EXCLUDED.geo_precision END,
    updated_at = now()
"""


def sync(rows: list[dict], *, retire_missing: bool = True) -> tuple[int, int]:
    """Make the registry agree with the catalogue. Returns (upserted, retired)."""
    refs = [r["external_ref"] for r in rows]
    with psycopg.connect(settings.dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT code, id FROM departments")
            dept_ids = {r[0]: r[1] for r in cur.fetchall()}
            fallback_dept = dept_ids.get(DEFAULT_DEPARTMENT)

            for row in rows:
                cur.execute(
                    _UPSERT,
                    {
                        **row,
                        "department_id": dept_ids.get(row["department_code"], fallback_dept),
                    },
                )

            retired = 0
            if retire_missing and refs:
                # Marked offline, never deleted: `sightings` references these
                # rows and they are evidence. A camera that comes back is
                # simply upserted online again on the next sync.
                cur.execute(
                    """
                    UPDATE cameras SET status = 'offline'::camera_status, updated_at = now()
                     WHERE external_ref LIKE %s
                       AND NOT (external_ref = ANY(%s))
                       AND status NOT IN ('offline'::camera_status,
                                          'decommissioned'::camera_status)
                    """,
                    # Decommissioned is excluded too. Until 14 Sep only 'offline'
                    # was, so the 31 cameras retired on 2 Sep would have been
                    # flipped back to 'offline' by the next sync, and so back
                    # into the estate, the health sweep and the map.
                    (f"{REF_PREFIX}-%", refs),
                )
                retired = cur.rowcount
        conn.commit()
    return len(rows), retired


def _load(args: argparse.Namespace) -> list[CatalogueCamera]:
    if args.catalogue_file:
        with open(args.catalogue_file, encoding="utf-8") as handle:
            return parse_catalogue(json.load(handle), base_url=BASE)
    return fetch_catalogue(BASE)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description="Sync the government feeds from the catalogue.")
    parser.add_argument("--dry-run", action="store_true", help="print, do not write")
    parser.add_argument(
        "--catalogue-file",
        help="read a saved catalogue response instead of the network, for offline work",
    )
    parser.add_argument(
        "--keep-missing",
        action="store_true",
        help="do not mark cameras absent from the catalogue as offline",
    )
    args = parser.parse_args()

    log.info("reading the ingest catalogue at %s", BASE)
    try:
        cameras = _load(args)
    except CatalogueAuthRequired as exc:
        # Reported separately, and with a different exit code, because it is a
        # different job to do next. "Unreachable" is waited out; this one is not
        # fixable from inside this repository at all.
        log.error("%s", exc)
        log.error("registry left untouched")
        return 2
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.error("catalogue unreachable (%s) — registry left untouched", exc)
        return 1

    if not cameras:
        log.error("the catalogue returned no cameras; refusing to retire the estate")
        return 1

    rows = [_row_for(c) for c in cameras]

    placed = sum(1 for r in rows if r["geo_precision"] != "unplaced")
    protocols = sorted({e.protocol for c in cameras for e in c.endpoints})
    codecs = sorted({c.codec for c in cameras if c.codec})
    log.info(
        "%d cameras — %d positioned (%d need a survey position), protocols %s, codecs %s",
        len(rows), placed, len(rows) - placed, ", ".join(protocols) or "none",
        ", ".join(codecs) or "unreported",
    )

    if args.dry_run:
        for row, camera in zip(rows, cameras, strict=True):
            endpoints = " ".join(e.protocol for e in camera.endpoints)
            print(
                f"{row['external_ref']:20} {row['name'][:32]:32} "
                f"{row['lat']:7.4f},{row['lon']:7.4f} {row['geo_precision']:9} "
                f"{row['district']:14} {row['kind']:7} "
                f"{camera.codec or '?':5} {camera.resolution or '?':10} [{endpoints}]"
            )
        return 0

    upserted, retired = sync(rows, retire_missing=not args.keep_missing)
    log.info(
        "registry synced: %d cameras onboarded or updated, %d no longer in the "
        "catalogue and marked offline", upserted, retired,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
