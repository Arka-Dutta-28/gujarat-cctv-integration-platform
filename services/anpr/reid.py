"""Vehicle appearance descriptors, for re-identification across cameras.

What this is for. A trace is built from plate reads, and a plate read can be
missing: the vehicle was too far, the angle was oblique, the plate was obscured,
or the OCR shed the read under load. Appearance gives a second, independent
handle on "is this the same vehicle", one that does not depend on reading
anything. It is also the only evidence that can contradict a plate: two
sightings of one registration whose vehicles look nothing alike is what a cloned
plate looks like from the other direction, and the journey plausibility check
sees the same case as an impossible speed.

What this is not, stated plainly. This is a colour-and-shape descriptor, not a
learned re-identification embedding. A trained re-ID model (OSNet and its
relatives) would be substantially better at telling two silver hatchbacks apart,
and could not be obtained here: this network measured about 132 kB/s and
truncated large downloads repeatedly, the same constraint that shaped every
model decision in M3. So the honest description is that it finds vehicles that
look alike, well enough to shortlist candidates for a human, and not well enough
to assert identity. The API says so, and the match is reported as a distance
rather than as a yes.

The plumbing is the part that generalises. Embeddings are pgvector columns with
a cosine index; swapping this function for a learned model is a change to one
function and a migration for the dimension, with no change to storage, query or
interface.

When this is computed. On the largest box of a track, alongside the
human-readable description in attributes.py: one frame, one budget rule, two
appearance measurements. It was not always so. Until 6 September 2026 this was
computed on the frame that read a plate best, which meant a vehicle whose plate
was never read carried no descriptor at all. That switched re-ID off on the 30
government cameras, which read essentially nothing and are precisely the cameras
this exists for. Measured after the fix, descriptor coverage on a night clip
went from 5 of 12 tracks to 11 of 12.

Why HSV rather than RGB. Vehicle colour under CCTV varies enormously in
brightness, since the same car is a different RGB at noon and under sodium
light, while hue is comparatively stable. Splitting hue, saturation and value
into separate histograms keeps that stability and lets brightness carry less
weight than colour, which is what the weights below do.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
from typing import Any

__all__ = [
    "EMBEDDING_DIM", "embed", "cosine_distance", "SIMILAR_DISTANCE", "APPEARANCE_DIM", "embed_many",
]

#: Histogram bins. 32 hue + 16 saturation + 8 value + 8 vertical colour profile,
#: which is 64 — small enough that an index over hundreds of millions of rows is
#: affordable, large enough to separate colours a person would call different.
HUE_BINS, SAT_BINS, VAL_BINS, BAND_BINS = 32, 16, 8, 8
EMBEDDING_DIM = HUE_BINS + SAT_BINS + VAL_BINS + BAND_BINS

#: Hue carries most of the signal; value (brightness) least, because it moves
#: with the weather and the time of day rather than with the vehicle.
WEIGHTS = {"hue": 1.0, "sat": 0.6, "val": 0.3, "band": 0.5}

#: Standard deviation in grey below which a crop is treated as a blank frame
#: rather than a vehicle. Real footage of even a black car at night carries
#: sensor noise well above this.
MIN_VARIATION = float(os.environ.get("REID_MIN_VARIATION", "2.0"))

#: Cosine distance below which two vehicles are worth showing to an operator as
#: possibly the same. Deliberately generous: the purpose is to shortlist for a
#: human, and a descriptor this simple cannot support a tighter claim.
SIMILAR_DISTANCE = float(os.environ.get("REID_SIMILAR_DISTANCE", "0.15"))


def embed(crop: Any) -> list[float] | None:
    """An appearance descriptor for one vehicle crop, L2-normalised.

    Returns None rather than raising: a sighting without a descriptor is a small
    loss, and an exception here would cost the read that produced it.
    """
    try:
        import cv2
        import numpy as np

        if crop is None or getattr(crop, "size", 0) == 0:
            return None

        # The lower half of a vehicle box is mostly road and shadow; the middle
        # band is bodywork. Cropping to it before histogramming stops the
        # tarmac from dominating the descriptor of every vehicle on the estate.
        height, width = crop.shape[:2]
        if height < 8 or width < 8:
            return None
        body = crop[int(height * 0.15) : int(height * 0.75), :]

        # A crop with no variation at all is a blank frame, not a vehicle: a
        # decode artefact, a dropped frame, or a lens that has been covered. It
        # would otherwise produce a perfectly respectable-looking descriptor —
        # a spike in the zeroth bin of every histogram — which would then match
        # every other blank frame in the estate and nothing real. A genuinely
        # dark vehicle is never uniform; sensor noise alone sees to that.
        grey = cv2.cvtColor(body, cv2.COLOR_BGR2GRAY)
        if float(grey.std()) < MIN_VARIATION:
            return None

        hsv = cv2.cvtColor(body, cv2.COLOR_BGR2HSV)
        hue = cv2.calcHist([hsv], [0], None, [HUE_BINS], [0, 180]).flatten()
        sat = cv2.calcHist([hsv], [1], None, [SAT_BINS], [0, 256]).flatten()
        val = cv2.calcHist([hsv], [2], None, [VAL_BINS], [0, 256]).flatten()

        # A coarse vertical profile of mean brightness. Cheap shape information:
        # it separates a dark van from a dark car with a bright windscreen band,
        # which the colour histograms alone cannot.
        bands = np.array_split(grey, BAND_BINS, axis=0)
        band = np.array([float(b.mean()) for b in bands], dtype=np.float32)

        vector = np.concatenate([
            _unit(hue) * WEIGHTS["hue"],
            _unit(sat) * WEIGHTS["sat"],
            _unit(val) * WEIGHTS["val"],
            _unit(band) * WEIGHTS["band"],
        ])
        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            return None
        return [float(x) for x in (vector / norm)]
    except Exception:  # noqa: BLE001 - never cost a read
        return None


def _unit(values: Any) -> Any:
    """Normalise one histogram so its own total, not its scale, is what counts."""
    import numpy as np

    total = float(np.sum(values))
    return values / total if total > 0 else np.zeros_like(values)


def cosine_distance(a: list[float], b: list[float]) -> float:
    """1 - cosine similarity, matching pgvector's `<=>` operator.

    Kept here so the tests and the reported figures use exactly the arithmetic
    the database uses, rather than something close to it.
    """
    if not a or not b or len(a) != len(b):
        return 1.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)


# --- learned appearance (DINOv2), 14 Sep 2026 ---------------------------------
#
# The histogram above cannot tell two white cars apart. A learned image model
# can do better.
# DINOv2-small (Meta, Apache-2.0, 22M parameters) was chosen over the vehicle
# re-ID checkpoints on the Hugging Face hub because those had no downloads and
# no stated source; DINOv2 is a well-tested general image model whose vectors
# are used for exactly this kind of "find the same object" search.
#
# One vector per track, from the largest view of the vehicle, computed in one
# batch when tracks finish, so it costs one model call per batch of sightings
# rather than one per frame.

APPEARANCE_DIM = 384
REID_MODEL = os.environ.get("REID_MODEL", "facebook/dinov2-small")
#: "auto" runs the model when transformers is installed (the GPU image), "off" never.
REID_LEARNED = os.environ.get("REID_LEARNED", "auto")

#: The average DINOv2 vector over 2,321 government-camera vehicle crops, subtracted
#: before normalising. Measured 14 Sep 2026: without it every vector shares a
#: large common part (the average has length 0.68), and the same bus minutes
#: apart scored 0.18, the same as two different cars on different cameras.
#: Centred, repeat views of one object fall below 0.05 and unrelated pairs sit
#: near 1.0.
MEAN_PATH = pathlib.Path(__file__).resolve().parents[2] / "data" / "reid-dinov2-mean.json"

_model: Any = None
_model_error: str | None = None
_mean: Any = None


def _load() -> Any:
    global _model, _model_error
    if _model is not None or _model_error is not None:
        return _model
    try:
        import torch
        from transformers import AutoImageProcessor, AutoModel

        device = "cuda" if torch.cuda.is_available() else "cpu"
        processor = AutoImageProcessor.from_pretrained(REID_MODEL)
        model = AutoModel.from_pretrained(REID_MODEL).to(device).eval()
        _model = (processor, model, device)
        global _mean
        _mean = torch.tensor(json.loads(MEAN_PATH.read_text())["mean"], device=device)
    except Exception as error:  # noqa: BLE001 - no model means no vector, never no sighting
        _model_error = f"{type(error).__name__}: {error}"
        import logging

        logging.getLogger("anpr.reid").warning("learned appearance off: %s", _model_error)
    return _model


def embed_many(crops: list[Any]) -> list[list[float] | None]:
    """One L2-normalised 384-number vector per vehicle crop (BGR), or None."""
    out: list[list[float] | None] = [None] * len(crops)
    usable = [i for i, c in enumerate(crops)
              if c is not None and getattr(c, "size", 0) and min(c.shape[:2]) >= 16]
    if not usable or REID_LEARNED == "off" or _load() is None:
        return out
    try:
        import torch

        processor, model, device = _model
        images = [crops[i][:, :, ::-1].copy() for i in usable]  # BGR → RGB
        # Squashed to 224×224, not centre-cropped: a centre crop cuts the front
        # and back off a vehicle seen side-on, which is most of what tells it apart.
        inputs = processor(images=images, return_tensors="pt", do_center_crop=False,
                           size={"height": 224, "width": 224}).to(device)
        with torch.no_grad():
            vectors = model(**inputs).pooler_output
        vectors = torch.nn.functional.normalize(vectors.float() - _mean, dim=1).cpu().tolist()
        for i, vector in zip(usable, vectors, strict=True):
            out[i] = vector
    except Exception:  # noqa: BLE001 - never cost a sighting
        import logging

        logging.getLogger("anpr.reid").exception("appearance batch failed")
    return out
