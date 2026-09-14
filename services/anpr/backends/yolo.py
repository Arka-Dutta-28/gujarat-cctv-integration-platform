"""Vehicle detection and tracking.

YOLO for detection, ByteTrack for association, both through ultralytics because
the alternative is re-implementing Kalman association for no benefit.

ByteTrack rather than a simpler IoU tracker for one specific reason: it keeps
low-confidence detections as association candidates instead of discarding them.
That is exactly the situation at night with headlight glare — the vehicle is
still there, the detector is just much less sure — and an IoU tracker drops the
track, which splits one vehicle into several and produces several sightings of
the same car seconds apart. Three of the four real feeds observed are night
scenes, so this is the normal case here rather than a refinement.

The model is loaded once per process and shared. Weights are cached under
`ANPR_MODEL_DIR` so a container restart does not re-download them, and so an
air-gapped deployment can be handed the directory.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from services.anpr.models import Box, VehicleDetection

log = logging.getLogger("anpr.yolo")

__all__ = ["YoloVehicleTracker", "model_dir", "device"]

#: Small by default. On a 20-core CPU with adaptive sampling this keeps up; on a
#: GPU it leaves headroom for many more cameras per card. Override for accuracy.
DEFAULT_MODEL = os.environ.get("ANPR_VEHICLE_MODEL", "yolo11n.pt")

#: Detection floor. Deliberately low — ByteTrack wants the weak detections, and
#: a vehicle that is only 0.3 confident is still a vehicle worth a plate read.
CONF_THRESHOLD = float(os.environ.get("ANPR_VEHICLE_CONF", "0.25"))

IMG_SIZE = int(os.environ.get("ANPR_IMG_SIZE", "640"))


def model_dir() -> str:
    return os.environ.get("ANPR_MODEL_DIR", "/data/models")


def device() -> str:
    """Where inference runs. `auto` picks CUDA when it is genuinely available.

    Explicit rather than implicit because the answer is a measured number in
    the performance evidence: a CPU figure quoted as a GPU one would be a false
    claim about the system's capacity.
    """
    requested = os.environ.get("ANPR_DEVICE", "auto").lower()
    if requested != "auto":
        return requested
    try:
        import torch

        return "cuda:0" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


_model_lock = threading.Lock()
_model: Any = None


def _shared_model() -> Any:
    """One model per process, loaded on first use."""
    global _model
    with _model_lock:
        if _model is None:
            from ultralytics import YOLO

            os.makedirs(model_dir(), exist_ok=True)
            os.environ.setdefault("YOLO_CONFIG_DIR", model_dir())
            path = os.path.join(model_dir(), DEFAULT_MODEL)
            _model = YOLO(path if os.path.exists(path) else DEFAULT_MODEL)
            log.info("loaded %s on %s", DEFAULT_MODEL, device())
        return _model


class YoloVehicleTracker:
    """Detects vehicles and keeps a stable id on each across frames.

    One tracker instance per camera: ultralytics holds the track state on the
    model object keyed by `persist`, so sharing one instance across cameras
    would associate a car in Rajkot with a lorry in Surat.
    """

    def __init__(self, conf: float = CONF_THRESHOLD, img_size: int = IMG_SIZE) -> None:
        self.conf = conf
        self.img_size = img_size
        self.device = device()
        self._model: Any = None
        self._names: dict[int, str] = {}

    def track(self, frame: Any) -> list[VehicleDetection]:
        if self._model is None:
            # Each camera gets its own model handle so track state cannot leak
            # between cameras; the weights themselves are shared by the loader.
            from ultralytics import YOLO

            shared = _shared_model()
            self._model = YOLO(shared.ckpt_path) if hasattr(shared, "ckpt_path") else shared
            self._names = dict(self._model.names)

        results = self._model.track(
            frame,
            persist=True,
            tracker="bytetrack.yaml",
            conf=self.conf,
            imgsz=self.img_size,
            device=self.device,
            verbose=False,
        )
        if not results:
            return []

        boxes = results[0].boxes
        if boxes is None or boxes.id is None:
            # Detections with no ids: the tracker has not associated anything
            # yet. Reported with track_id None so the pipeline skips them
            # rather than inventing an association.
            return [
                VehicleDetection(
                    box=_box(xyxy), track_id=None,
                    label=self._names.get(int(cls), "unknown"), confidence=float(conf),
                )
                for xyxy, cls, conf in zip(
                    boxes.xyxy.tolist() if boxes is not None else [],
                    boxes.cls.tolist() if boxes is not None else [],
                    boxes.conf.tolist() if boxes is not None else [],
                    strict=False,
                )
            ]

        return [
            VehicleDetection(
                box=_box(xyxy),
                track_id=str(int(track_id)),
                label=self._names.get(int(cls), "unknown"),
                confidence=float(conf),
            )
            for xyxy, track_id, cls, conf in zip(
                boxes.xyxy.tolist(), boxes.id.tolist(), boxes.cls.tolist(),
                boxes.conf.tolist(), strict=False,
            )
        ]

    def reset(self) -> None:
        """Drop track state — a reconnected stream is not a continuation."""
        self._model = None


def _box(xyxy: list[float]) -> Box:
    x1, y1, x2, y2 = (int(round(v)) for v in xyxy[:4])
    return Box(x1, y1, x2, y2)
