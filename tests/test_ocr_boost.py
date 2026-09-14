"""OCR boost: operator-chosen cameras read with PaddleOCR-VL for a while.

Pinned here: the request rules, the status an operator sees, the worker's
polling cache, the one-reader-per-process rule, and the per-camera swap,
including what is written back when the model will not load.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from services.anpr import ocr_boost
from services.anpr.backends.learned_ocr import PaddleVlPlateOcr
from services.anpr.worker import CameraWorker
from services.api.routers.ocr_boost import BoostRequest, boost_status

# --- the API's rules --------------------------------------------------------


def test_a_request_names_cameras_or_a_place_not_both_or_neither() -> None:
    assert BoostRequest(camera_ids=["c1"]).minutes == 60
    assert BoostRequest(near={"lat": 23.0, "lon": 72.5, "radius_m": 800}).near is not None
    for bad in ({}, {"camera_ids": ["c1"], "near": {"lat": 23.0, "lon": 72.5}},
                {"camera_ids": []}, {"camera_ids": ["c1"], "minutes": 0},
                {"camera_ids": ["c1"], "minutes": 721}, {"camera_ids": ["c1"], "backend": "qwen"}):
        with pytest.raises(ValidationError):
            BoostRequest(**bad)


def test_status_says_the_most_final_thing_first() -> None:
    now = datetime(2026, 9, 14, 12, tzinfo=UTC)
    row = {"cleared_at": None, "expires_at": now + timedelta(minutes=5),
           "apply_error": None, "applied_at": None}
    assert boost_status(row, now) == "pending"
    assert boost_status({**row, "applied_at": now}, now) == "running"
    assert boost_status({**row, "applied_at": now, "apply_error": "no GPU"}, now) == "failed"
    assert boost_status({**row, "applied_at": now, "expires_at": now}, now) == "expired"
    assert boost_status({**row, "cleared_at": now, "expires_at": now}, now) == "cleared"


# --- the worker's cache and shared readers ----------------------------------


def test_cache_refreshes_on_its_interval_and_keeps_the_last_copy_on_failure() -> None:
    calls = []

    def loader(connect, ids):  # noqa: ANN001, ANN202
        calls.append(ids)
        if len(calls) == 2:
            raise RuntimeError("database blip")
        return {"c1": (7, "paddleocr-vl")} if len(calls) == 1 else {}

    cache = ocr_boost.BoostCache(connect=None, camera_ids=["c1", "c2"], refresh_s=5, loader=loader)
    assert cache.wanted("c1", now=0) == (7, "paddleocr-vl")
    assert cache.wanted("c1", now=4) == (7, "paddleocr-vl") and len(calls) == 1
    assert cache.wanted("c1", now=5) == (7, "paddleocr-vl"), "a failed poll must not end a boost"
    assert cache.wanted("c1", now=10) is None and cache.wanted("c2", now=10) is None


class _Reader:
    def __init__(self, error: str | None = None) -> None:
        self.error = error

    def ensure_loaded(self) -> str | None:
        return self.error


def test_one_reader_per_backend_per_process() -> None:
    built = []
    readers = ocr_boost.SharedReaders(lambda b: built.append(b) or _Reader())
    first, _ = readers.get("paddleocr-vl")
    second, _ = readers.get("paddleocr-vl")
    assert first is second and built == ["paddleocr-vl"]


def test_a_reader_that_will_not_load_is_not_rebuilt_every_frame() -> None:
    built = []
    readers = ocr_boost.SharedReaders(lambda b: built.append(b) or _Reader("no GPU"), retry_s=300)
    assert readers.get("paddleocr-vl", now=0) == (None, "no GPU")
    assert readers.get("paddleocr-vl", now=100) == (None, "no GPU") and len(built) == 1
    readers.get("paddleocr-vl", now=301)
    assert len(built) == 2, "retried after retry_s, so a GPU that comes back is used"


def test_paddleocr_vl_refuses_to_run_without_a_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANPR_OCR_DEVICE", "cpu")
    monkeypatch.delenv("ANPR_PADDLE_VL_ALLOW_CPU", raising=False)
    assert "needs a GPU" in (PaddleVlPlateOcr().ensure_loaded() or "")


# --- the per-camera swap ----------------------------------------------------


class _Boosts:
    def __init__(self) -> None:
        self.value: tuple[int, str] | None = None

    def wanted(self, camera_id: str) -> tuple[int, str] | None:
        return self.value


def _worker(readers: ocr_boost.SharedReaders) -> SimpleNamespace:
    w = SimpleNamespace(
        camera=SimpleNamespace(id="c1", path="cam-01"), pipeline=SimpleNamespace(ocr="docTR"),
        boosts=_Boosts(), readers=readers, connect=None,
        boost_id=None, boost_reader=None, pre_boost_ocr=None,
    )
    w.apply = lambda: CameraWorker._apply_boost(w)
    return w


def test_a_boost_swaps_the_reader_and_restores_it_when_it_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = []
    monkeypatch.setattr(ocr_boost, "report", lambda c, i, e: reports.append((i, e)))
    paddle = _Reader()
    w = _worker(ocr_boost.SharedReaders(lambda b: paddle))

    w.apply()
    assert w.pipeline.ocr == "docTR" and reports == []

    w.boosts.value = (7, "paddleocr-vl")
    w.apply()
    assert w.pipeline.ocr is paddle and reports == [(7, None)]

    w.pipeline.ocr = "escalated hub"  # escalation swaps underneath the boost
    w.apply()
    assert w.pipeline.ocr is paddle, "the operator's request wins while it lasts"

    w.boosts.value = None
    w.apply()
    assert w.pipeline.ocr == "escalated hub" and w.boost_reader is None
    assert reports == [(7, None)]


def test_a_boost_that_cannot_run_is_reported_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = []
    monkeypatch.setattr(ocr_boost, "report", lambda c, i, e: reports.append((i, e)))
    w = _worker(ocr_boost.SharedReaders(lambda b: _Reader("PaddleOCR-VL needs a GPU")))
    w.boosts.value = (8, "paddleocr-vl")
    w.apply()
    w.apply()
    assert w.pipeline.ocr == "docTR"
    assert reports == [(8, "PaddleOCR-VL needs a GPU")], "reported once, not every frame"


def test_escalation_never_swaps_a_learned_reader_for_the_hub_model() -> None:
    from services.anpr.escalation import Rung

    class _Reader(PaddleVlPlateOcr):
        pass

    reader = _Reader()
    counted = []
    w = SimpleNamespace(
        camera=SimpleNamespace(path="sentinel-cam01"),
        pipeline=SimpleNamespace(ocr=reader, locator="classical", tracker="motion"),
        escalation=SimpleNamespace(observe=lambda completed: Rung("ocr", "heavy", "test")),
        metrics=SimpleNamespace(count=counted.append),
    )
    CameraWorker._maybe_escalate(w, [object()])
    assert w.pipeline.ocr is reader and counted == ["escalations_skipped"]
