"""Draw synthetic faces, so face analytics can be demonstrated safely.

Every face this produces is drawn from primitives — ellipses, circles, arcs. No
photograph is used, no generative model is used, and no face here resembles any
real person, because none was involved at any point.

That is the whole reason the file exists. Demonstrating face analytics on
footage of real people means processing their biometric data, which raises live
questions under the DPDP Act 2023 and needs a lawful basis this project does not
have. Drawn faces have no data subject, so the demonstration costs nobody
anything.

They are crude, and that is fine: the claim being demonstrated is that a second
analytics module plugs into the same pipeline, not that any particular detector
is accurate. A cascade detector finds these because they carry the coarse
light/dark structure it keys on — two dark eye regions above a lighter midface.

Usage:
    python -m scripts.make_synthetic_faces --out data/synthetic-faces --count 12
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import random
import sys

log = logging.getLogger("synthetic-faces")

WIDTH, HEIGHT = 480, 360


def draw_face(rng: random.Random, width: int = WIDTH, height: int = HEIGHT):
    """One frame containing one drawn face. Returns a BGR image."""
    import cv2
    import numpy as np

    # A flat, mid-tone background: the point is the face, and a busy background
    # would make this a test of the detector rather than of the plumbing.
    shade = rng.randint(90, 150)
    img = np.full((height, width, 3), shade, dtype=np.uint8)

    cx, cy = width // 2, height // 2
    fw, fh = rng.randint(70, 105), rng.randint(95, 130)
    skin = rng.randint(170, 225)

    cv2.ellipse(img, (cx, cy), (fw, fh), 0, 0, 360, (skin, skin, skin), -1)

    # Eyes: two dark regions side by side, which is the structure a Haar
    # cascade's first stages actually key on.
    eye_dy = fh // 4
    eye_dx = fw // 2
    eye_r = max(6, fw // 9)
    for sign in (-1, 1):
        cv2.circle(img, (cx + sign * eye_dx, cy - eye_dy), eye_r, (40, 40, 40), -1)
        cv2.circle(img, (cx + sign * eye_dx, cy - eye_dy), max(2, eye_r // 2),
                   (15, 15, 15), -1)
        # A brow above each eye deepens the contrast the detector looks for.
        cv2.ellipse(img, (cx + sign * eye_dx, cy - eye_dy - eye_r - 4),
                    (eye_r + 3, 4), 0, 180, 360, (60, 60, 60), -1)

    # Nose and mouth: a lighter midface between the dark eyes and a dark mouth
    # line is the other structure the cascade uses.
    nose = max(4, fw // 10)
    cv2.ellipse(img, (cx, cy + fh // 12), (nose, nose * 2), 0, 0, 360,
                (skin - 25, skin - 25, skin - 25), -1)
    cv2.ellipse(img, (cx, cy + fh // 2 - 8), (fw // 3, max(5, fh // 14)),
                0, 0, 180, (70, 50, 50), -1)

    return img


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
    parser = argparse.ArgumentParser(description="Draw synthetic faces for the FRS demo.")
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("data/synthetic-faces"))
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--seed", type=int, default=20260831)
    args = parser.parse_args()

    try:
        import cv2  # noqa: F401
    except ImportError:
        log.error("OpenCV is needed to draw these. Run inside the anpr image.")
        return 1
    import cv2

    rng = random.Random(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    for i in range(args.count):
        path = args.out / f"synthetic-face-{i:02d}.png"
        cv2.imwrite(str(path), draw_face(rng))
    log.info("wrote %d drawn faces to %s — no real person is depicted",
             args.count, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
