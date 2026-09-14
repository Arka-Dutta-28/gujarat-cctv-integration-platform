"""The opt-in learned OCR backends: selection, input handling, and read shape.

The models themselves need PyTorch and are measured outside this suite
(`services/anpr/backends/learned_ocr.py`); these tests pin the code around them.
"""

import importlib.util

import numpy as np
import pytest

from services.anpr.backends import build_ocr
from services.anpr.backends import learned_ocr as L
from services.anpr.backends.tesseract import TesseractPlateOcr


def test_auto_is_doctr_when_installed_else_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name == "doctr" else real(name, *a))
    assert isinstance(build_ocr("auto"), TesseractPlateOcr)

    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: object() if name == "doctr" else real(name, *a))
    assert isinstance(build_ocr("auto"), L.DoctrPlateOcr)


def test_tesseract_can_still_be_forced() -> None:
    assert isinstance(build_ocr("tesseract"), TesseractPlateOcr)


@pytest.mark.parametrize(
    ("choice", "module"), [("paddleocr-vl", "transformers"), ("doctr", "doctr")]
)
def test_refuses_to_build_without_its_library(choice: str, module: str) -> None:
    if importlib.util.find_spec(module) is not None:
        pytest.skip(f"{module} is installed here")
    with pytest.raises(ModuleNotFoundError):
        build_ocr(choice)


def test_prepare_enlarges_small_crops_to_96_px_rgb() -> None:
    out = L.prepare(np.zeros((24, 80, 3), np.uint8))
    assert out.shape == (96, 320, 3)


def test_prepare_never_shrinks() -> None:
    assert L.prepare(np.zeros((120, 300), np.uint8)).shape == (120, 300, 3)


def test_clean_keeps_only_plate_characters() -> None:
    assert L.clean("-gj 08.cs5454\n") == "GJ08CS5454"


class _Fake(L._Lazy):
    def __init__(self, text: str, confidence: float = 0.9, fail: bool = False) -> None:
        super().__init__()
        self.text, self.confidence, self.fail, self.loads = text, confidence, fail, 0

    def _load(self) -> object:
        self.loads += 1
        if self.fail:
            raise RuntimeError("no weights")
        return object()

    def _infer(self, model: object, rgb: np.ndarray) -> tuple[str, float]:
        assert rgb.shape[0] == 96
        return self.text, self.confidence


def test_read_cleans_and_loads_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANPR_OCR_DEVICE", "cpu")
    ocr = _Fake("gj 01 ab 1234", 0.81234)
    crop = np.zeros((20, 60, 3), np.uint8)
    first, second = ocr.read(crop), ocr.read(crop)
    assert first is not None and first.text == "GJ01AB1234" and first.confidence == 0.8123
    assert second is not None and ocr.loads == 1


def test_a_sentence_is_not_a_plate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANPR_OCR_DEVICE", "cpu")
    crop = np.zeros((20, 60, 3), np.uint8)
    assert _Fake("The image is too blurry to recognize any text content").read(crop) is None
    assert _Fake("GJ 18 BH 1234 A").read(crop) is not None  # 11 characters still a plate


def test_nothing_readable_is_none_not_an_empty_plate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANPR_OCR_DEVICE", "cpu")
    assert _Fake(" - ").read(np.zeros((20, 60, 3), np.uint8)) is None


def test_a_model_that_will_not_load_reads_nothing_and_stops_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANPR_OCR_DEVICE", "cpu")
    ocr = _Fake("GJ01AB1234", fail=True)
    crop = np.zeros((20, 60, 3), np.uint8)
    assert ocr.read(crop) is None and ocr.read(crop) is None
    assert ocr.loads == 1


def test_every_camera_shares_one_reader_per_kind() -> None:
    class _Kind(L._Lazy):
        pass

    first, second = L.shared(_Kind), L.shared(_Kind)
    assert first is second, "one model per process, not one per camera"
    assert L.shared(L.DoctrPlateOcr) is not first
