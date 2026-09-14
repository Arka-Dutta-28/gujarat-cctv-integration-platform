"""Tesseract OCR result handling.

The image processing needs OpenCV and is exercised live; what is tested here is
the part that decides what a read is *worth*, because that number feeds straight
into the per-track vote and from there into every confidence figure in the
submission.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.anpr.backends.tesseract import TesseractPlateOcr, assemble


class FakePytesseract:
    Output = SimpleNamespace(DICT="dict")

    def __init__(self, data: dict, version: str = "5.3.0") -> None:
        self.data = data
        self.version = version

    def get_tesseract_version(self) -> str:
        return self.version

    def image_to_data(self, image: object, **kwargs: object) -> dict:  # noqa: ARG002
        return self.data


class TestReading:
    def test_a_clean_read_is_returned_with_its_confidence(self) -> None:
        r = assemble({"text": ["GJ01AB1234"], "conf": ["92"]})
        assert r is not None
        assert r.text == "GJ01AB1234"
        assert r.confidence == 0.92

    def test_fragments_are_joined(self) -> None:
        """Tesseract routinely splits a plate at the state/RTO gap."""
        r = assemble({"text": ["GJ01", "AB", "1234"], "conf": ["90", "88", "95"]})
        assert r is not None
        assert r.text == "GJ01AB1234"

    def test_confidence_is_the_weakest_fragment_not_the_mean(self) -> None:
        """One unreadable group must not hide behind two clear ones."""
        r = assemble({"text": ["GJ01", "AB", "1234"], "conf": ["95", "30", "95"]})
        assert r is not None
        assert r.confidence == 0.30

    def test_invented_punctuation_is_stripped(self) -> None:
        """The plate border reads as punctuation more often than not."""
        r = assemble({"text": ["|GJ-01", "AB.1234|"], "conf": ["80", "80"]})
        assert r is not None
        assert r.text == "GJ01AB1234"

    def test_lowercase_is_normalised_up(self) -> None:
        r = assemble({"text": ["gj01ab1234"], "conf": ["80"]})
        assert r is not None
        assert r.text == "GJ01AB1234"

    def test_nothing_readable_returns_nothing(self) -> None:
        """Returning an empty string would create a sighting out of noise."""
        assert assemble({"text": ["", "  ", "!!"], "conf": ["-1", "0", "10"]}) is None

    def test_rejected_fragments_do_not_contribute_confidence(self) -> None:
        r = assemble({"text": ["GJ01AB1234", "!!"], "conf": ["85", "-1"]})
        assert r is not None
        assert r.confidence == 0.85

    def test_a_malformed_confidence_does_not_crash_the_read(self) -> None:
        r = assemble({"text": ["GJ01AB1234", "XX"], "conf": ["85", None]})
        assert r is not None
        assert r.text == "GJ01AB1234"


class TestAvailability:
    def test_a_missing_engine_reads_nothing_rather_than_guessing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        monkeypatch.setitem(sys.modules, "pytesseract", None)
        assert TesseractPlateOcr().read(object()) is None

    def test_availability_is_only_probed_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys

        fake = FakePytesseract({"text": ["GJ01AB1234"], "conf": ["90"]})
        monkeypatch.setitem(sys.modules, "pytesseract", fake)
        reader = TesseractPlateOcr()
        reader.read(object())
        assert reader._available is True
