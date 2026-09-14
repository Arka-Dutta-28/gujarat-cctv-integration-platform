"""Seed the registry with the simulated NH-48 camera farm.

Idempotent: keyed on `external_ref`, so `make seed` can be re-run safely and the
`seed` compose service can sit in the default `up` path.

The geography is real. Cameras are placed along the actual NH-48 alignment from
Ahmedabad through Vadodara to Surat, which means OSRM (M4) snaps journeys to a
road network instead of drawing straight lines across farmland, and the implied
speeds between consecutive cameras are physically meaningful.

Usage:
    python -m scripts.seed --cameras
    python -m scripts.seed --cameras --count 50 --reset
"""

from __future__ import annotations

import argparse
import logging
import sys

from services.common.config import settings
from services.common.db import wait_for_db
from services.common.geo import Point, haversine_m, initial_bearing_deg, interpolate_polyline

log = logging.getLogger("seed")

# --- NH-48, Ahmedabad -> Surat -------------------------------------------
# Waypoints traced along the real carriageway, south-bound. Roughly 230 km.
NH48_CORRIDOR: list[Point] = [
    Point(22.9757, 72.5966),  # Ahmedabad — Narol junction
    Point(22.8620, 72.6280),  # Bareja
    Point(22.7500, 72.6850),  # Kheda
    Point(22.6916, 72.8420),  # Nadiad
    Point(22.5645, 72.9289),  # Anand
    Point(22.4450, 73.0620),  # Vasad
    Point(22.3072, 73.1812),  # Vadodara
    Point(22.1750, 73.1640),  # Vadodara south bypass
    Point(22.0553, 73.1250),  # Karjan
    Point(21.9020, 72.9990),  # Palej
    Point(21.7051, 72.9959),  # Bharuch
    Point(21.6279, 73.0143),  # Ankleshwar
    Point(21.5300, 72.9800),  # Panoli
    Point(21.4676, 72.9558),  # Kosamba
    Point(21.3466, 72.9648),  # Kim
    Point(21.2740, 72.9550),  # Kamrej
    Point(21.1702, 72.8311),  # Surat — Kadodara
]

# Which district a camera falls in, by fraction of the way down the corridor.
DISTRICT_BANDS: list[tuple[float, str]] = [
    (0.09, "Ahmedabad"),
    (0.18, "Kheda"),
    (0.30, "Anand"),
    (0.46, "Vadodara"),
    (0.72, "Bharuch"),
    (1.01, "Surat"),
]

# The five departments the evaluation dataset actually comes from (organiser
# FAQ: "50 cameras deployed across five Government departments"). Matching these
# exactly means the department filter demoes against real category names rather
# than invented ones.
DEPARTMENTS: list[tuple[str, str]] = [
    ("Health", "HEALTH"),
    ("Police", "POLICE"),
    ("GSRTC", "GSRTC"),
    ("Panchayat", "PANCHAYAT"),
    ("Municipal Corporation", "MUNICIPAL"),
]

# --- heterogeneity --------------------------------------------------------
# The real estate is a mix of vintages and vendors, and at least one camera is
# always terrible. Discovering the pipeline's failure modes against this farm is
# far cheaper than discovering them on site (build-plan §2.5).
#
# (profile, width, height, fps, codec, weight, extra ffmpeg args)
PROFILES: list[tuple[str, int, int, float, str, int, list[str]]] = [
    ("hd_h264", 1920, 1080, 25.0, "h264", 12, []),
    ("hd_h265", 1920, 1080, 20.0, "hevc", 6, []),
    ("sd_h264", 1280, 720, 15.0, "h264", 16, []),
    ("low_h264", 640, 480, 10.0, "h264", 10, []),
    # An older vendor unit still on MPEG-4 Part 2 — exercises the decoder path.
    ("legacy_mpeg4", 704, 576, 12.0, "mpeg4", 4, []),
    # The deliberately awful one: quarter-VGA, 4 fps, heavy compression and
    # sensor noise. If ANPR copes with this, the rest of the farm is easy.
    ("degraded", 320, 240, 4.0, "h264", 2, ["-b:v", "120k", "-vf", "noise=alls=18:allf=t"]),
]

# Average corridor speed used to derive playback offsets. Deliberately well
# under the 120 km/h implausibility threshold: a synthetic journey that trips
# our own clone detector would be a self-inflicted demo failure.
CORRIDOR_SPEED_KMH = 72.0

MOUNT_HEIGHTS = [5.5, 6.0, 6.5, 7.0, 8.0]
FOV_BY_PROFILE = {"hd_h264": 60, "hd_h265": 55, "sd_h264": 65, "low_h264": 70,
                  "legacy_mpeg4": 75, "degraded": 80}
RANGE_BY_PROFILE = {"hd_h264": 80, "hd_h265": 90, "sd_h264": 60, "low_h264": 40,
                    "legacy_mpeg4": 35, "degraded": 25}


def _weighted_profiles(count: int) -> list[tuple]:
    """Deterministic profile assignment, spread evenly rather than clustered."""
    pool: list[tuple] = []
    for entry in PROFILES:
        pool.extend([entry] * entry[5])
    return [pool[(i * 7) % len(pool)] for i in range(count)]


def _district_for(fraction: float) -> str:
    for upper, name in DISTRICT_BANDS:
        if fraction < upper:
            return name
    return DISTRICT_BANDS[-1][1]


#: The clip that carries the planted trace vehicle, and the cameras it is
#: assigned to. See `_apply_journey_corridor` for why this is special-cased.
JOURNEY_CLIP = "journey-corridor-day.mp4"
JOURNEY_FIRST_CAMERA = 20
JOURNEY_CAMERAS = 6


def _apply_journey_corridor(farm: list[dict], video_files: list[str]) -> list[str]:
    """Give one run of consecutive cameras a shared clip and a real stagger.

    The round-robin assignment above is right for background traffic and wrong
    for a journey, and the failure was invisible until M4 asked for one. Two
    cameras showing the same clip are `len(video_files)` hops apart — twelve
    cameras, 57 km — and the simulator seeks `offset_s % duration`, so the
    2,856 s offset of the next camera sharing a clip becomes 66 s against a 90 s
    file. Both halves of the stagger were destroyed, and the visible symptom was
    one plate reported at two cameras 57 km apart in the same second.

    So the trace vehicle gets a corridor of *consecutive* cameras, all playing
    one long clip, with offsets that decrease by one hop and never exceed the
    clip's duration. Offsets decrease because a larger offset means the stream
    started further into the clip, so the planted pass has already gone by —
    the vehicle reaches the low-offset camera later, which is downstream.

    Returns the external_refs it claimed, so the caller can report them: the
    planted plate and its corridor are demo inputs and belong in PROGRESS.
    """
    if JOURNEY_CLIP not in video_files:
        return []

    claimed: list[str] = []
    corridor = [
        c for c in farm
        if JOURNEY_FIRST_CAMERA <= int(c["external_ref"].split("-")[1])
        < JOURNEY_FIRST_CAMERA + JOURNEY_CAMERAS
    ]
    if len(corridor) < 2:
        return []

    # The arithmetic, written out because it is easy to get backwards.
    #
    # A camera with offset O shows clip position (O + W) mod D at wall time W,
    # so a pass planted at clip second P is seen at W = (P - O) mod D. For the
    # vehicle to travel *downstream*, W must increase along the corridor, which
    # means O must decrease. Taking the last camera as the reference:
    #
    #     O_k = offset_last - offset_k
    #
    # gives the upstream camera the largest offset and the downstream one zero,
    # spaced by exactly the travel time already encoded in the seeded offsets —
    # so if the corridor is ever re-seeded at a different density, the stagger
    # follows it rather than a constant here.
    #
    # This only holds inside one clip cycle, which requires the planted pass to
    # sit at least (n-1) hops into the clip; otherwise the first camera's
    # appearance time wraps past the last one's and the journey runs backwards.
    # `build_journey_clip` places it accordingly.
    original = [c["offset_s"] for c in corridor]
    for camera, offset in zip(corridor, original, strict=True):
        camera["source_file"] = JOURNEY_CLIP
        camera["offset_s"] = original[-1] - offset
        claimed.append(camera["external_ref"])
    return claimed


def build_farm(count: int, video_files: list[str]) -> list[dict]:
    """Compute the full camera farm. Pure — no database, so it is testable."""
    # The corridor clip is twenty-five minutes of near-empty road carrying one
    # planted vehicle. It is not background traffic, and letting the round-robin
    # hand it to arbitrary cameras would both starve them of vehicles and scatter
    # the planted plate across the estate — which is the bug this whole corridor
    # exists to fix, reintroduced by the back door.
    background = [f for f in video_files if f != JOURNEY_CLIP]
    positions = interpolate_polyline(NH48_CORRIDOR, count)
    profiles = _weighted_profiles(count)

    # Cumulative distance drives the playback offsets, so a vehicle appears at
    # successive cameras at successive, physically plausible times.
    cumulative_m = [0.0]
    for i in range(1, count):
        cumulative_m.append(cumulative_m[-1] + haversine_m(positions[i - 1], positions[i]))

    farm: list[dict] = []
    for i, pos in enumerate(positions):
        profile, width, height, fps, codec, _weight, extra = profiles[i]

        # Point the camera down the corridor: bearing toward the next camera,
        # or back along the last leg for the final one. This is not decoration —
        # it drives coverage polygons and route plausibility.
        if i < count - 1:
            bearing = initial_bearing_deg(pos, positions[i + 1])
        else:
            bearing = initial_bearing_deg(positions[i - 1], pos)

        fraction = cumulative_m[i] / cumulative_m[-1] if cumulative_m[-1] else 0.0
        offset_s = int(round(cumulative_m[i] / 1000.0 / CORRIDOR_SPEED_KMH * 3600.0))

        farm.append(
            {
                "external_ref": f"cam-{i + 1:02d}",
                "name": f"NH-48 {_district_for(fraction)} KM{cumulative_m[i] / 1000:.0f}",
                "department_code": DEPARTMENTS[i % len(DEPARTMENTS)][1],
                # A handful of private, public-facing cameras: the platform is
                # meant to ingest those too, and the distinction carries
                # different consent and retention rules.
                "ownership_type": "private_public_facing" if i % 11 == 5 else "government",
                "adapter": "rtsp",
                "stream_ref": f"{settings.rtsp_base}/cam-{i + 1:02d}",
                "lat": pos.lat,
                "lon": pos.lon,
                "district": _district_for(fraction),
                "address": (
                    f"NH-48 chainage {cumulative_m[i] / 1000:.1f} km, "
                    f"{_district_for(fraction)}"
                ),
                "bearing": bearing,
                "fov_degrees": FOV_BY_PROFILE[profile],
                "range_m": RANGE_BY_PROFILE[profile],
                "mounting_height_m": MOUNT_HEIGHTS[i % len(MOUNT_HEIGHTS)],
                "retention_days": 30 if i % 3 else 90,
                # --- simulator side ---
                "source_file": background[i % len(background)] if background else "",
                "offset_s": offset_s,
                "profile": profile,
                "width": width,
                "height": height,
                "fps": fps,
                "codec": codec,
                "extra_args": extra,
            }
        )

    _apply_journey_corridor(farm, video_files)
    return farm


def discover_video_files() -> list[str]:
    from pathlib import Path

    d = Path(settings.sim_video_dir)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.suffix.lower() in {".mp4", ".mkv", ".ts"})


def seed_corridor() -> None:
    """Store the NH-48 alignment as a corridor row.

    Gap analysis needs a route to measure against, and holding corridors as data
    rather than as a constant in application code means an operator can add one
    without a deploy.
    """
    import psycopg

    line = ", ".join(f"{p.lon} {p.lat}" for p in NH48_CORRIDOR)
    with psycopg.connect(settings.dsn) as conn:
        conn.execute(
            """
            INSERT INTO corridors (name, description, geom)
            VALUES (%s, %s, ST_GeogFromText(%s))
            ON CONFLICT (name) DO UPDATE SET geom = EXCLUDED.geom,
                                             description = EXCLUDED.description
            """,
            (
                "NH-48 Ahmedabad–Surat",
                "National Highway 48 between Ahmedabad (Narol) and Surat (Kadodara).",
                f"SRID=4326;LINESTRING({line})",
            ),
        )
        conn.commit()
    log.info("seeded corridor NH-48 Ahmedabad–Surat")


# --- watchlist -----------------------------------------------------------

#: The planted **alerting** plate. Distinct from the M4 trace plate on purpose:
#: the two graded capabilities are separate code paths, and a demo that proved
#: both with one vehicle would not have shown that. This one is in the
#: watchlist; `GJ18TR4321` deliberately is not.
#:
#: Chosen from the generated clips' ground-truth manifest rather than invented,
#: so the alert is provable end to end — the manifest says the vehicle passed,
#: the sighting says the platform read it, the alert says the watchlist caught
#: it. It rides `traffic-10-day.mp4`, which is on three cameras in three
#: districts, so the console shows the same vehicle moving rather than one
#: camera repeating itself.
ALERT_PLATE = "GJ05UV9972"

#: The rest exist so the console is not a single row. Categories and sources are
#: the real ones — VAHAN for the stolen-vehicle database, eGujCop for the state
#: police system — because an evaluator recognises them and a made-up source
#: would undercut the integration claim the rest of the platform is making.
WATCHLIST_SEED: list[dict] = [
    {"plate": ALERT_PLATE, "category": "stolen", "severity": 5, "source": "VAHAN",
     "case_ref": "FIR-0142/2026",
     "notes": "Planted demo vehicle — ground truth in data/test-videos/manifest.json"},
    {"plate": "GJ01AB1234", "category": "wanted", "severity": 4, "source": "eGujCop",
     "case_ref": "CR-88/2026", "notes": "Representative entry"},
    {"plate": "GJ18KL4477", "category": "suspect", "severity": 2, "source": "manual",
     "notes": "Representative entry"},
    {"plate": "MH12QQ8080", "category": "blacklisted", "severity": 3, "source": "VAHAN",
     "notes": "Out-of-state entry — the format check accepts it, alerting matches it"},
]


def seed_watchlist() -> int:
    """Load the demo watchlist. Idempotent, keyed on the normalised plate.

    Normalisation happens here with the same function the pipeline uses, so a
    seeded entry and a read of the same vehicle land on one key. Seeding a raw
    string would be the one place in the platform where invariant 3 did not
    apply, and the symptom would be an alert that silently never fires.
    """
    import psycopg

    from services.common.plates import normalise_plate

    with psycopg.connect(settings.dsn) as conn:
        for entry in WATCHLIST_SEED:
            conn.execute(
                """
                INSERT INTO watchlist (plate, plate_normalised, category, severity,
                                       source, case_ref, notes)
                VALUES (%s, %s, %s::wl_category, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (entry["plate"], normalise_plate(entry["plate"]), entry["category"],
                 entry["severity"], entry["source"], entry.get("case_ref"),
                 entry.get("notes")),
            )
        conn.commit()
        seeded = conn.execute("SELECT count(*) FROM watchlist").fetchone()[0]
    log.info("watchlist holds %d entries; planted alerting plate %s", seeded, ALERT_PLATE)
    return seeded


def seed_cameras(count: int, reset: bool = False) -> int:
    import psycopg

    video_files = discover_video_files()
    if not video_files:
        log.warning(
            "no clips in %s — cameras will be registered but the simulator has "
            "nothing to publish. Run `make videos` first.",
            settings.sim_video_dir,
        )

    farm = build_farm(count, video_files)

    with psycopg.connect(settings.dsn) as conn:
        with conn.cursor() as cur:
            if reset:
                log.warning("--reset: removing existing simulated cameras")
                cur.execute("DELETE FROM camera_sim_config")
                cur.execute("DELETE FROM cameras WHERE external_ref LIKE 'cam-%%'")

            for name, code in DEPARTMENTS:
                cur.execute(
                    "INSERT INTO departments (name, code) VALUES (%s, %s)"
                    " ON CONFLICT (code) DO NOTHING",
                    (name, code),
                )

            cur.execute("SELECT code, id FROM departments")
            dept_ids = {r[0]: r[1] for r in cur.fetchall()}

            for cam in farm:
                cur.execute(
                    """
                    INSERT INTO cameras (
                        external_ref, name, department_id, ownership_type,
                        adapter, stream_ref, geom, address, district,
                        bearing, fov_degrees, range_m, mounting_height_m,
                        status, retention_days, commissioned_on
                    ) VALUES (
                        %(external_ref)s, %(name)s, %(department_id)s,
                        %(ownership_type)s::ownership,
                        %(adapter)s::adapter_type, %(stream_ref)s,
                        ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
                        %(address)s, %(district)s,
                        %(bearing)s, %(fov_degrees)s, %(range_m)s, %(mounting_height_m)s,
                        'unknown'::camera_status, %(retention_days)s, CURRENT_DATE
                    )
                    ON CONFLICT (external_ref) DO UPDATE SET
                        name = EXCLUDED.name,
                        department_id = EXCLUDED.department_id,
                        ownership_type = EXCLUDED.ownership_type,
                        adapter = EXCLUDED.adapter,
                        stream_ref = EXCLUDED.stream_ref,
                        geom = EXCLUDED.geom,
                        address = EXCLUDED.address,
                        district = EXCLUDED.district,
                        bearing = EXCLUDED.bearing,
                        fov_degrees = EXCLUDED.fov_degrees,
                        range_m = EXCLUDED.range_m,
                        mounting_height_m = EXCLUDED.mounting_height_m,
                        retention_days = EXCLUDED.retention_days
                    RETURNING id
                    """,
                    {**cam, "department_id": dept_ids[cam["department_code"]]},
                )
                camera_id = cur.fetchone()[0]

                cur.execute(
                    """
                    INSERT INTO camera_sim_config (
                        camera_id, source_file, offset_s, profile,
                        width, height, fps, codec, extra_args
                    ) VALUES (
                        %(camera_id)s, %(source_file)s, %(offset_s)s, %(profile)s,
                        %(width)s, %(height)s, %(fps)s, %(codec)s, %(extra_args)s
                    )
                    ON CONFLICT (camera_id) DO UPDATE SET
                        source_file = EXCLUDED.source_file,
                        offset_s = EXCLUDED.offset_s,
                        profile = EXCLUDED.profile,
                        width = EXCLUDED.width,
                        height = EXCLUDED.height,
                        fps = EXCLUDED.fps,
                        codec = EXCLUDED.codec,
                        extra_args = EXCLUDED.extra_args
                    """,
                    {**cam, "camera_id": camera_id},
                )
        conn.commit()

    log.info("seeded %d cameras along NH-48 (%d source clips)", len(farm), len(video_files))
    return len(farm)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description="Seed reference and test data.")
    parser.add_argument("--cameras", action="store_true", help="seed the NH-48 camera farm")
    parser.add_argument("--corridor", action="store_true", help="seed the NH-48 corridor line")
    parser.add_argument("--watchlist", action="store_true", help="seed the demo watchlist")
    parser.add_argument("--count", type=int, default=settings.sim_camera_count)
    parser.add_argument(
        "--reset", action="store_true", help="delete simulated cameras before seeding"
    )
    args = parser.parse_args()

    if not (args.cameras or args.corridor or args.watchlist):
        parser.error("nothing to do; pass --cameras, --corridor and/or --watchlist")

    wait_for_db()
    if args.corridor:
        seed_corridor()
    if args.cameras:
        seed_cameras(args.count, reset=args.reset)
    if args.watchlist:
        seed_watchlist()
    return 0


if __name__ == "__main__":
    sys.exit(main())
