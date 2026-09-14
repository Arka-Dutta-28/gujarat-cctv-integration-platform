"""Synthetic Indian plate crops, degraded to look like the government cameras.

Stage 1 of the recogniser training plan. Renders a plate we chose, so the label
is perfect, then damages it until it resembles what the harvested crops actually
look like. The damage is not guessed: the ranges below come from 143 real crops
in `data/corpus` (plate-likeness band >= 2, excluding the 41 hand-verified test
plates and every crop a human marked "not a plate"), measured 14 Sep 2026:

    width px        p5 31  p25 54  p50 86  p75 113  p95 139
    aspect w/h      p5 2.0 p25 2.5 p50 2.8 p75 3.2  p95 4.2   (crops carry margin)
    mean luma       p5 63  p25 91  p50 98  p75 122  p95 143
    contrast (std)  p5 13  p25 32  p50 56  p75 89   p95 102
    night           35 of 143

Images are generated on the fly rather than written to disk: there is no
dataset to store, move or keep in step with this file. `preview` writes one
sheet so a person can check the fakes look like the real thing before any
training is trusted.

    python -m scripts.synth_plates preview --out /tmp/synth.png
"""

from __future__ import annotations

import argparse
import random
from functools import cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from scripts.make_test_videos import OTHER_STATES, SERIES_LETTERS

#: Narrow bold sans faces, all under licences that allow training (Bitstream
#: Vera/DejaVu, OFL, Apache 2.0). The official plate typeface is not installed
#: anywhere here, so the generator varies the face instead of pretending one is it.
FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-CondensedBold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-SemiCondensedBold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-ExtraCondensedBold.ttf",
    "/usr/share/fonts/truetype/roboto/unhinted/RobotoCondensed-Bold.ttf",
]

CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

#: Recogniser input. Height 32 is ~4x the character height the cameras give us;
#: width 128 holds an 11-character plate with room for CTC's blank steps.
INPUT_H, INPUT_W = 32, 128

# Measured percentiles from the docstring, sampled piecewise-uniformly.
_WIDTH = [31, 54, 86, 113, 139]
_ASPECT = [2.0, 2.5, 2.8, 3.2, 4.2]
_LUMA = [63, 91, 98, 122, 143]
_CONTRAST = [13, 32, 56, 89, 102]
NIGHT_FRACTION = 35 / 143


def _from_percentiles(rng: random.Random, points: list[float]) -> float:
    """Draw from p5..p95 so each quartile band keeps its measured share."""
    band = rng.choices(range(4), weights=[20, 25, 25, 20])[0]
    return rng.uniform(points[band], points[band + 1])


def random_plate(rng: random.Random) -> str:
    """A structurally valid mark. Unlike the corridor clips, every RTO is fair.

    `make_test_videos.random_plate` draws from 8 Gujarat RTO codes because it
    decorates one corridor; a recogniser trained on that would learn that
    `GJ35` is unlikely, and the verified test plates include GJ35, GJ09, GJ11.
    """
    if rng.random() < 0.75:
        state, rto = "GJ", f"{rng.randint(1, 39):02d}"
    else:
        state, rto = rng.choice(OTHER_STATES), f"{rng.randint(1, 99):02d}"
    series = "".join(rng.choice(SERIES_LETTERS) for _ in range(rng.choice([1, 2, 2, 2, 2])))
    return f"{state}{rto}{series}{rng.randint(1, 9999):04d}"


def _spaced(plate: str, rng: random.Random) -> str:
    """How the mark is painted: `GJ01AB1234`, `GJ 01 AB 1234`, `GJ01 AB 1234`."""
    state, rto, rest = plate[:2], plate[2:4], plate[4:]
    series, number = rest[:-4], rest[-4:]
    style = rng.random()
    if style < 0.35:
        return plate
    if style < 0.8:
        return f"{state} {rto} {series} {number}"
    return f"{state}{rto} {series} {number}"


@cache
def _font(path: str) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, 48)


def render(plate: str, rng: random.Random) -> np.ndarray:
    """A clean plate on a little background, as a grey uint8 image."""
    font = _font(rng.choice(FONTS))
    text = _spaced(plate, rng)
    left, top, right, bottom = font.getbbox(text)
    tw, th = right - left, bottom - top
    ind = rng.random() < 0.6  # the blue IND strip on high-security plates
    pad_x, pad_y = rng.randint(8, 16), rng.randint(6, 12)
    strip = 26 if ind else 0
    pw, ph = tw + 2 * pad_x + strip, th + 2 * pad_y

    yellow = rng.random() < 0.15  # commercial vehicles
    plate_img = Image.new("RGB", (pw, ph), (235, 200, 40) if yellow else (238, 238, 232))
    d = ImageDraw.Draw(plate_img)
    d.rectangle([1, 1, pw - 2, ph - 2], outline=(20, 20, 20), width=rng.randint(2, 4))
    if ind:
        d.rectangle([5, 5, strip, ph - 6], fill=(40, 70, 150))
    d.text((strip + pad_x - left, pad_y - top), text, font=font, fill=(15, 15, 15))

    # Margin around the plate, because harvested crops are loose (aspect 2-4
    # against a plate's ~4.5): bumper above and below, sometimes grille.
    aspect = _from_percentiles(rng, _ASPECT)
    canvas_w = int(pw * rng.uniform(1.0, 1.25))
    canvas_h = max(ph, int(canvas_w / aspect))
    tone = rng.randint(30, 200)
    bg = np.full((canvas_h, canvas_w, 3), tone, np.uint8)
    bg = cv2.add(bg, np.random.default_rng(rng.randint(0, 2**31)).integers(
        0, 25, bg.shape, dtype=np.uint8))
    x = rng.randint(0, canvas_w - pw)
    y = rng.randint(0, canvas_h - ph)
    bg[y:y + ph, x:x + pw] = np.asarray(plate_img)
    return cv2.cvtColor(bg, cv2.COLOR_RGB2GRAY)


def degrade(img: np.ndarray, rng: random.Random, night: bool, clean_scale: int = 0):
    """Damage a clean render until it looks like a government-camera crop.

    With ``clean_scale`` (super-resolution training) it also returns the same
    plate, same tilt, same brightness, *undamaged* at ``clean_scale`` times the
    damaged size: ``(damaged, clean)``. The clean copy uses no random draws, so
    passing it changes nothing about the damaged image a given seed produces.
    """
    # Pad first so the warp below cannot push a character out of frame. A
    # recogniser trained on clipped fakes learns to drop characters.
    pad = int(0.08 * img.shape[1])
    img = cv2.copyMakeBorder(img, pad // 2, pad // 2, pad, pad, cv2.BORDER_REPLICATE)
    h, w = img.shape
    # Small perspective: cameras look down and across the lane.
    jitter = 0.03 * w
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = src + np.float32([[rng.uniform(-jitter, jitter), rng.uniform(-jitter / 3, jitter / 3)]
                            for _ in range(4)])
    img = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (w, h),
                              borderMode=cv2.BORDER_REPLICATE)
    geometry = img
    if rng.random() < 0.5:
        k = rng.choice([3, 5, 7])
        kernel = np.zeros((k, k), np.float32)
        kernel[k // 2, :] = 1.0 / k  # horizontal motion
        img = cv2.filter2D(img, -1, kernel)
    # Down to the size the cameras actually deliver — the step that matters most.
    out_w = int(_from_percentiles(rng, _WIDTH))
    # Blur scaled to what survives: a 35 px crop blurred at sigma 2 before
    # shrinking is noise with a label, and teaches guessing, not reading.
    img = cv2.GaussianBlur(img, (0, 0), rng.uniform(0.3, 1.0 + min(1.0, out_w / 110)))
    out_h = max(8, int(round(out_w * h / w)))
    img = cv2.resize(img, (out_w, out_h), interpolation=rng.choice(
        [cv2.INTER_AREA, cv2.INTER_LINEAR, cv2.INTER_NEAREST]))
    if rng.random() < 0.4:
        # Blocky: the camera saw even fewer pixels and the evidence crop was
        # enlarged from them. Several real crops look exactly like this.
        k = rng.uniform(1.8, 3.2)
        small = cv2.resize(img, (max(4, int(out_w / k)), max(3, int(out_h / k))),
                           interpolation=cv2.INTER_AREA)
        img = cv2.resize(small, (out_w, out_h), interpolation=cv2.INTER_NEAREST)

    # Brightness and contrast drawn from the measured crops.
    target_mean = _from_percentiles(rng, _LUMA) * (0.55 if night else 1.0)
    target_std = _from_percentiles(rng, _CONTRAST) * (0.7 if night else 1.0)
    f = img.astype(np.float32)
    f = (f - f.mean()) / (f.std() + 1e-3) * target_std + target_mean
    clean = None
    if clean_scale:
        c = cv2.resize(geometry, (out_w * clean_scale, out_h * clean_scale),
                       interpolation=cv2.INTER_AREA).astype(np.float32)
        c = (c - c.mean()) / (c.std() + 1e-3) * target_std + target_mean
        clean = np.clip(c, 0, 255).astype(np.uint8)
    noise_rng = np.random.default_rng(rng.randint(0, 2**31))
    f += noise_rng.normal(0, rng.uniform(2, 14 if night else 7), f.shape)
    img = np.clip(f, 0, 255).astype(np.uint8)

    # Codec damage: evidence images are JPEG of an H.264 frame.
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, rng.randint(15, 75)])
    damaged = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE) if ok else img
    return (damaged, clean) if clean_scale else damaged


def to_input(img: np.ndarray) -> np.ndarray:
    """Any grey crop to the recogniser's fixed input, stretched to fill, float 0-1.

    Real crops and synthetic ones go through this same function, so the model never
    sees a preprocessing difference between training and test.

    Stretched, not letterboxed (14 Sep 2026). The first run kept the aspect ratio
    and padded the right. Fake crops carry margin, so their ink ended about 60% of
    the way across, while tight real crops fill the width. The model learned that
    characters never sit in the right third and, on real plates, read the start and
    stopped: 27 of 42 test reads were shorter than the plate (GJ18EA for
    GJ18EA4944). Stretching puts every crop's content across the full width,
    whatever its margin.
    """
    resized = cv2.resize(img, (INPUT_W, INPUT_H), interpolation=cv2.INTER_CUBIC)
    return resized.astype(np.float32) / 255.0


def sample(rng: random.Random) -> tuple[np.ndarray, str]:
    """One training example: (degraded grey crop, label)."""
    plate = random_plate(rng)
    return degrade(render(plate, rng), rng, night=rng.random() < NIGHT_FRACTION), plate


def pair(rng: random.Random, scale: int = 4) -> tuple[np.ndarray, np.ndarray, str]:
    """One super-resolution example: (damaged crop, clean crop x scale, label)."""
    plate = random_plate(rng)
    damaged, clean = degrade(render(plate, rng), rng, night=rng.random() < NIGHT_FRACTION,
                             clean_scale=scale)
    return damaged, clean, plate


def preview(out: Path, real_dir: Path | None, n: int = 24, seed: int = 0) -> None:
    """Fakes on the left, real crops on the right, same display height."""
    rng = random.Random(seed)
    tile_h, tile_w = 48, 200

    def tile(img: np.ndarray, text: str) -> np.ndarray:
        h, w = img.shape
        s = min(tile_h / h, tile_w / w)
        im = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))),
                        interpolation=cv2.INTER_NEAREST)
        t = np.zeros((tile_h + 16, tile_w), np.uint8)
        t[:im.shape[0], :im.shape[1]] = im
        cv2.putText(t, text, (2, tile_h + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.38, 255, 1)
        return t

    fakes = [tile(*sample(rng)) for _ in range(n)]
    reals = []
    if real_dir is not None:
        paths = sorted(real_dir.glob("*.png")) if real_dir.is_dir() else [
            Path(line) for line in real_dir.read_text().split()]
        for p in random.Random(seed).sample(paths, min(n, len(paths))):
            reals.append(tile(cv2.imread(str(p), cv2.IMREAD_GRAYSCALE), "real " + p.stem))
    cols = [np.vstack(fakes)]
    if reals:
        cols.append(np.full((cols[0].shape[0], 8), 128, np.uint8))
        cols.append(np.vstack(reals + [np.zeros_like(fakes[0])] * (n - len(reals))))
    cv2.imwrite(str(out), np.hstack(cols))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["preview"])
    parser.add_argument("--out", type=Path, default=Path("synth-preview.png"))
    parser.add_argument("--real", type=Path, default=None,
                        help="real crops to show beside the fakes: a folder, or a file of paths")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    preview(args.out, args.real, seed=args.seed)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
