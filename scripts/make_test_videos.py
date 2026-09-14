"""Generate synthetic traffic clips with known plates.

Real dashcam footage would be better input, but it comes with no ground truth.
These clips are generated, so we know exactly which plate crossed which frame at
which second — and that manifest is what lets M3 report an ANPR accuracy figure
we can actually defend, rather than an impression.

Each clip renders vehicles approaching the camera: a coloured body, a white
plate panel, and the registration mark in a plate-like mono face. Deliberately
plain — the point is a controllable, repeatable signal, not photorealism.

Usage:
    python -m scripts.make_test_videos --count 12 --duration 90
    python -m scripts.make_test_videos --list-plates
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from services.common.plates import normalise_plate

log = logging.getLogger("make-videos")

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
]

# Gujarat RTO codes, so the seeded farm reads plates that belong on this corridor.
GJ_RTO = ["01", "05", "06", "18", "21", "23", "27", "38"]
OTHER_STATES = ["MH", "RJ", "MP", "DL", "KA", "UP"]
SERIES_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I or O: not used on real series

VEHICLE_COLOURS = [
    ("white", "0xEDEDED"),
    ("silver", "0xB8BCC0"),
    ("black", "0x1C1C1E"),
    ("red", "0xB4231F"),
    ("blue", "0x24457A"),
    ("grey", "0x6E7276"),
]

# One vehicle every this many seconds. Loose enough that plates do not overlap
# on screen, dense enough that 50 streams generate a useful sighting volume.
PASS_INTERVAL_S = 6.0
PASS_TRAVEL_S = 5.0


@dataclass
class Pass:
    """One vehicle crossing the frame — a row of ANPR ground truth."""

    plate: str
    plate_normalised: str
    start_s: float
    end_s: float
    colour: str
    lane: int


@dataclass
class Clip:
    filename: str
    duration_s: float
    width: int
    height: int
    fps: float
    passes: list[Pass]
    # Real feeds are mostly night scenes with severe headlight bloom (see
    # docs/field-observations.md §4). Accuracy has to be reported per condition,
    # so the condition is ground truth and lives in the manifest.
    condition: str = "day"


def _font() -> str:
    for path in FONT_CANDIDATES:
        if Path(path).is_file():
            return path
    raise FileNotFoundError(
        "no mono font found; install fonts-dejavu-core or edit FONT_CANDIDATES"
    )


def random_plate(rng: random.Random) -> str:
    """A structurally valid Indian mark, weighted toward Gujarat."""
    if rng.random() < 0.8:
        state, rto = "GJ", rng.choice(GJ_RTO)
    else:
        state, rto = rng.choice(OTHER_STATES), f"{rng.randint(1, 45):02d}"
    series = "".join(rng.choice(SERIES_LETTERS) for _ in range(rng.choice([1, 2, 2, 2])))
    return f"{state}{rto}{series}{rng.randint(0, 9999):04d}"


def build_clip(
    index: int,
    duration_s: float,
    width: int,
    height: int,
    fps: float,
    rng: random.Random,
    planted: list[str] | None = None,
    condition: str = "day",
) -> Clip:
    """Plan one clip's contents. Pure — rendering happens separately."""
    passes: list[Pass] = []
    planted = list(planted or [])

    t = 1.0
    while t + PASS_TRAVEL_S < duration_s:
        # Planted plates go in first so they are guaranteed to appear; the rest
        # is background traffic.
        plate = planted.pop(0) if planted else random_plate(rng)
        colour_name, _ = rng.choice(VEHICLE_COLOURS)
        passes.append(
            Pass(
                plate=plate,
                plate_normalised=normalise_plate(plate),
                start_s=round(t, 2),
                end_s=round(t + PASS_TRAVEL_S, 2),
                colour=colour_name,
                lane=rng.choice([0, 1]),
            )
        )
        t += PASS_INTERVAL_S * rng.uniform(0.8, 1.25)

    return Clip(
        filename=f"traffic-{index:02d}-{condition}.mp4",
        duration_s=duration_s,
        width=width,
        height=height,
        fps=fps,
        passes=passes,
        condition=condition,
    )


#: Plates planted for the M4 trace. Not in the watchlist — the trace path must
#: be provable without alerting having fired, since the evaluation hands over a
#: registration number for a vehicle nobody was watching.
JOURNEY_PLATE = "GJ18TR4321"

#: How sparse the journey clip's background traffic is. A corridor clip runs for
#: twenty-odd minutes, and at the normal six-second spacing that would be two
#: hundred vehicle sprites in one filter graph — enough to make ffmpeg's graph
#: the bottleneck rather than the content. The journey clip exists to carry one
#: plate past a run of cameras, so its traffic only has to be present.
JOURNEY_PASS_INTERVAL_S = 45.0


#: How much of a hop the planted pass sits *beyond* the last camera's offset,
#: as a multiple of `hop_s`. See `build_journey_clip` — this is the upstream
#: camera's whole margin between its stream starting and the pass going by, and
#: at the previous value (20 seconds flat) that camera routinely missed it.
JOURNEY_START_MARGIN_S = 1.0


def build_journey_clip(
    duration_s: float,
    width: int,
    height: int,
    fps: float,
    rng: random.Random,
    plate: str = JOURNEY_PLATE,
    appear_at_s: float | None = None,
    condition: str = "day",
    hop_s: float = 238.0,
    cameras: int = 6,
) -> Clip:
    """A long, sparse clip carrying one known plate at a known second.

    This exists because the camera farm could not produce a genuine journey, and
    the reason is worth writing down. Cameras sit 4.74 km apart with playback
    offsets stepping 238 s — exactly one hop at 72 km/h, which is right. But
    clips are assigned round-robin across twelve of them, so two cameras showing
    the same clip are twelve hops (57 km) apart, and the offset is applied
    *modulo the clip duration*, so a 2,856 s offset against a 90 s clip becomes
    66 s. The stagger that made the journey plausible was being thrown away, and
    the visible result was one plate appearing at two cameras 57 km apart in the
    same second — which our own clone detector then flagged, correctly, 128
    times.

    The constraint the harness has to respect is `duration >= cameras x hop`.
    One clip, long enough not to wrap, assigned to a run of *consecutive*
    cameras with decreasing offsets, gives a vehicle that crosses the corridor
    once at a plausible speed.
    """
    # The planted pass must sit at least (cameras - 1) hops into the clip. A
    # camera with offset O sees clip second P at wall time (P - O) mod duration;
    # place P earlier than the largest offset and the first camera's sighting
    # wraps past the last one's, so the trace runs backwards down the corridor.
    #
    # The margin above that floor is not cosmetic, and 20 s was not enough.
    # The *upstream* camera carries the largest offset, so it sees the pass at
    # `P - max(offset)` — which is exactly this margin, measured from the moment
    # its stream starts. At 20 s the pass went by while the estate was still
    # connecting: measured 7 Sep, cam-20 was expected 38 s after stream start,
    # missed it, and caught the following cycle instead. The trace then showed
    # cam-21 through cam-23 spaced correctly at 221 s and 237 s with cam-20
    # stranded, and the plausibility check correctly refused to call it one
    # journey.
    #
    # One hop is the principled floor: it is the longest any camera already
    # waits between neighbours, so it cannot make the clip shorter than the
    # constraint above already requires, and it is an order of magnitude more
    # than the estate takes to connect and warm its models.
    if appear_at_s is None:
        appear_at_s = (cameras - 1) * hop_s + JOURNEY_START_MARGIN_S * hop_s

    passes: list[Pass] = [
        Pass(
            plate=plate,
            plate_normalised=normalise_plate(plate),
            start_s=round(appear_at_s, 2),
            end_s=round(appear_at_s + PASS_TRAVEL_S, 2),
            colour="white",
            lane=0,
        )
    ]

    t = appear_at_s + JOURNEY_PASS_INTERVAL_S
    while t + PASS_TRAVEL_S < duration_s:
        background = random_plate(rng)
        # Background traffic must never accidentally be the planted plate, or
        # the trace would show a vehicle that was two vehicles.
        if background == plate:
            t += JOURNEY_PASS_INTERVAL_S
            continue
        colour_name, _ = rng.choice(VEHICLE_COLOURS)
        passes.append(
            Pass(
                plate=background,
                plate_normalised=normalise_plate(background),
                start_s=round(t, 2),
                end_s=round(t + PASS_TRAVEL_S, 2),
                colour=colour_name,
                lane=rng.choice([0, 1]),
            )
        )
        t += JOURNEY_PASS_INTERVAL_S * rng.uniform(0.8, 1.25)

    return Clip(
        filename=f"journey-corridor-{condition}.mp4",
        duration_s=duration_s,
        width=width,
        height=height,
        fps=fps,
        passes=passes,
        condition=condition,
    )


def _filtergraph(clip: Clip, font: str) -> str:
    """Build the ffmpeg filter_complex that renders every vehicle pass.

    Each vehicle is composited as its own sprite rather than drawn straight onto
    the frame. That is not stylistic. `drawbox` in ffmpeg 6.x does not
    re-evaluate its x/y expressions per frame and exposes no `eval` option to
    make it, so a moving box freezes at its first position while `drawtext` —
    which does evaluate per frame — keeps moving. The visible symptom is plate
    text sliding down an empty road, leaving its panel and car behind.

    Rendering each car into a static sprite and moving it with `overlay` (whose
    position *is* evaluated per frame) sidesteps this entirely: nothing moves
    inside a sprite, so init-time evaluation is exactly what we want there.
    """
    w, h = clip.width, clip.height
    colour_hex = dict(VEHICLE_COLOURS)
    night = clip.condition == "night"

    body_w, body_h = int(w * 0.30), int(h * 0.30)
    plate_w, plate_h = int(body_w * 0.62), max(14, int(body_h * 0.22))
    font_size = max(10, int(plate_h * 0.74))
    plate_x = (body_w - plate_w) // 2
    plate_y = int(body_h * 0.66)

    background = "0x0E1012" if night else "0x38393B"
    travel = h + body_h + 80
    rate = travel / PASS_TRAVEL_S

    chains: list[str] = [
        f"color=c={background}:s={w}x{h}:r={clip.fps}:d={clip.duration_s}[bg0]",
        # Lane divider — static, so drawing it onto the background is fine.
        f"[bg0]drawbox=x={w // 2 - 2}:y=0:w=4:h={h}:color=0xC8C8A0@0.55:t=fill[bg]",
    ]

    # --- one sprite per vehicle pass ---
    for i, p in enumerate(clip.passes):
        sprite = [
            f"color=c={colour_hex[p.colour]}:s={body_w}x{body_h}"
            f":r={clip.fps}:d={clip.duration_s}",
            # Windscreen band, so the body is not one flat rectangle.
            f"drawbox=x=8:y=8:w={body_w - 16}:h={int(body_h * 0.30)}"
            f":color=0x2A3038@1:t=fill",
        ]
        if night:
            # Headlights either side of the plate. The bloom pass turns these
            # into the wash that swallows vehicles in the real night feeds.
            lamp_w, lamp_h = max(10, body_w // 7), max(8, body_h // 9)
            lamp_y = int(body_h * 0.62)
            for lamp_x in (int(body_w * 0.10), body_w - int(body_w * 0.10) - lamp_w):
                sprite.append(
                    f"drawbox=x={lamp_x}:y={lamp_y}:w={lamp_w}:h={lamp_h}"
                    f":color=0xFFF6D8@1:t=fill"
                )
        # Plate panel: white ground, dark border, black text — as on a real one.
        sprite.append(
            f"drawbox=x={plate_x}:y={plate_y}:w={plate_w}:h={plate_h}"
            f":color=white@1:t=fill"
        )
        sprite.append(
            f"drawbox=x={plate_x}:y={plate_y}:w={plate_w}:h={plate_h}"
            f":color=0x101010@1:t=2"
        )
        sprite.append(
            f"drawtext=fontfile={font}:text='{p.plate}':fontcolor=black"
            f":fontsize={font_size}:x={plate_x + 5}"
            f":y={plate_y + max(1, (plate_h - font_size) // 2)}"
        )
        chains.append(",".join(sprite) + f"[car{i}]")

    # --- composite the sprites onto the background ---
    prev = "bg"
    for i, p in enumerate(clip.passes):
        lane_x = int(w * (0.28 if p.lane == 0 else 0.62)) - body_w // 2
        y_expr = f"-{body_h + 40}+(t-{p.start_s})*{rate:.3f}"
        chains.append(
            f"[{prev}][car{i}]overlay=x={lane_x}:y='{y_expr}'"
            f":enable='between(t,{p.start_s},{p.end_s})'[s{i}]"
        )
        prev = f"s{i}"

    # --- burnt-in overlays, exactly as the real feeds carry them ---
    # Present on purpose: the OCR stage must prove it does not read a timestamp
    # band or a site label as a plate (docs/field-observations.md §6).
    label = clip.filename.split("-")[1]
    overlays = (
        f"drawtext=fontfile={font}:text='%{{pts\\:hms}}':fontcolor=white@0.85"
        f":fontsize=16:x=8:y=8:box=1:boxcolor=black@0.45:boxborderw=4,"
        f"drawtext=fontfile={font}:text='CSITMS-{label}_SIM':fontcolor=white@0.8"
        f":fontsize=18:x=w-tw-10:y=8"
    )

    if night:
        # Bloom: pull the highlights out, blur them hard, screen them back.
        #
        # `gbrp`, not `rgb24`, and the distinction is the whole bug. Screening
        # in YUV blends the chroma planes as if they were brightness, which
        # drives U and V to their extremes and turns every frame magenta. Asking
        # for packed rgb24 does not prevent that: `blend` does not accept it, so
        # ffmpeg silently auto-inserts a scaler that converts back to YUV right
        # before the blend. Planar RGB is a format blend takes directly, so no
        # conversion is inserted and the screen happens per colour channel.
        chains.append(f"[{prev}]{overlays},format=gbrp,split[base][hi]")
        chains.append(
            "[hi]lutrgb=r='if(gt(val,190),val,0)':g='if(gt(val,190),val,0)'"
            ":b='if(gt(val,190),val,0)',gblur=sigma=20[bloom]"
        )
        chains.append(
            "[base][bloom]blend=all_mode=screen:all_opacity=0.9,format=yuv420p[out]"
        )
    else:
        chains.append(f"[{prev}]{overlays},format=yuv420p[out]")

    return ";".join(chains)


def render(clip: Clip, out_dir: Path, font: str, overwrite: bool = False) -> Path:
    out = out_dir / clip.filename
    if out.exists() and not overwrite:
        log.info("%s exists, skipping", clip.filename)
        return out

    # The graph generates its own background and vehicle sprites, so there is no
    # input file — hence -filter_complex with an explicit [out] rather than -vf.
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-filter_complex", _filtergraph(clip, font),
        "-map", "[out]",
        "-t", str(clip.duration_s),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
        "-pix_fmt", "yuv420p",
        # Frequent keyframes: RTSP subscribers should get a picture quickly, and
        # the M3 decoder seeks on these.
        "-g", str(int(clip.fps * 2)),
        str(out),
    ]
    log.info("rendering %s (%.0fs, %d passes)", clip.filename, clip.duration_s, len(clip.passes))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed for {clip.filename}:\n{proc.stderr[-2000:]}")
    return out


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description="Generate synthetic ANPR test clips.")
    parser.add_argument("--count", type=int, default=12, help="number of distinct clips")
    parser.add_argument("--duration", type=float, default=90.0, help="seconds per clip")
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=540)
    parser.add_argument("--fps", type=float, default=15.0)
    parser.add_argument("--out", default="data/test-videos")
    parser.add_argument("--seed", type=int, default=20260818, help="keeps clips reproducible")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--list-plates", action="store_true", help="print the manifest's plates and exit"
    )
    parser.add_argument(
        "--journey-cameras", type=int, default=6,
        help="Corridor length for the planted-journey clip, in cameras.",
    )
    parser.add_argument(
        "--journey-hop", type=float, default=238.0,
        help="Seconds between consecutive corridor cameras (4.74 km at 72 km/h).",
    )
    parser.add_argument(
        "--no-journey", action="store_true",
        help="Skip the corridor clip. It is long to render.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out)
    manifest_path = out_dir / "manifest.json"

    if args.list_plates:
        if not manifest_path.is_file():
            print("no manifest; generate clips first", file=sys.stderr)
            return 1
        data = json.loads(manifest_path.read_text())
        for clip in data["clips"]:
            for p in clip["passes"]:
                print(f"{clip['filename']}  {p['start_s']:7.2f}s  {p['plate']}")
        return 0

    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found on PATH", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    font = _font()
    rng = random.Random(args.seed)

    # Night dominates the real estate, so it dominates the test set too.
    night_share = 0.6
    clips = [
        build_clip(
            i + 1, args.duration, args.width, args.height, args.fps, rng,
            condition="night" if i < round(args.count * night_share) else "day",
        )
        for i in range(args.count)
    ]
    if not args.no_journey:
        # duration >= cameras x hop, or the offsets wrap and the journey stops
        # being a journey. The margin carries the pass itself plus a little slack.
        needed = args.journey_cameras * args.journey_hop + 120.0
        clips.append(
            build_journey_clip(
                needed, args.width, args.height, args.fps, rng,
                hop_s=args.journey_hop, cameras=args.journey_cameras,
            )
        )
        log.info(
            "journey clip: %.0fs for %d cameras at %.0fs per hop, plate %s",
            needed, args.journey_cameras, args.journey_hop, JOURNEY_PLATE,
        )

    for clip in clips:
        render(clip, out_dir, font, overwrite=args.overwrite)

    manifest = {
        "generated_with_seed": args.seed,
        "clips": [asdict(c) for c in clips],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))

    total = sum(len(c.passes) for c in clips)
    distinct = len({p.plate for c in clips for p in c.passes})
    log.info(
        "%d clips, %d vehicle passes, %d distinct plates -> %s",
        len(clips), total, distinct, manifest_path,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
