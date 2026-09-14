"""The optional face-analytics module — and, mostly, what it refuses to do.

This module exists to demonstrate one architectural claim: that the edge
pipeline takes analytics modules as plugins rather than having ANPR welded into
it. A claim with one implementation behind it is an assertion; a second working
module is evidence.

Most of what is tested here is therefore *restraint*, because that is where the
risk is. A face module that quietly defaults to on, or that stops plates being
read when its model is missing, or that reports a number an operator would read
as "probability this is a person", would each be worse than not shipping one.
"""

from __future__ import annotations

import pytest

from services.analytics.frs import (
    CASCADE_CANDIDATES,
    FaceAnalytic,
    FaceDetection,
    frs_enabled,
)


class TestDisabledByDefault:
    """"Ships disabled" is only true if the default is genuinely off."""

    def test_absent_variable_means_off(self, monkeypatch) -> None:
        monkeypatch.delenv("FRS_ENABLED", raising=False)
        assert frs_enabled() is False

    @pytest.mark.parametrize("value", ["", " ", "0", "false", "no", "off", "maybe", "2"])
    def test_nothing_but_an_explicit_affirmative_enables_it(self, monkeypatch, value) -> None:
        """Including values that look truthy to a shell or to `bool()`.

        `bool("false")` is True in Python, and a config typo must not be the
        thing that switches face recognition on across an estate.
        """
        monkeypatch.setenv("FRS_ENABLED", value)
        assert frs_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " True "])
    def test_explicit_affirmatives_enable_it(self, monkeypatch, value) -> None:
        monkeypatch.setenv("FRS_ENABLED", value)
        assert frs_enabled() is True

    def test_the_analytic_refuses_to_load_when_disabled(self, monkeypatch) -> None:
        monkeypatch.delenv("FRS_ENABLED", raising=False)
        analytic = FaceAnalytic()
        assert analytic.available is False
        assert analytic.unavailable_reason == "FRS_ENABLED is not set"

    def test_it_is_read_per_call_not_cached_at_import(self, monkeypatch) -> None:
        """Turning it off must take effect without a rebuild, and no import
        order may leave a stale True behind."""
        monkeypatch.setenv("FRS_ENABLED", "true")
        assert frs_enabled() is True
        monkeypatch.setenv("FRS_ENABLED", "false")
        assert frs_enabled() is False


class TestCannotBreakAnpr:
    """An optional analytic sharing a worker with the mandatory one must never
    be able to stop plates being read."""

    def test_detect_returns_empty_when_disabled(self, monkeypatch) -> None:
        monkeypatch.delenv("FRS_ENABLED", raising=False)
        # Never reaches OpenCV, so an object with no image API is a fair stand-in.
        assert FaceAnalytic().detect(object()) == []

    def test_a_missing_cascade_is_unavailable_not_an_exception(self, monkeypatch) -> None:
        monkeypatch.setenv("FRS_ENABLED", "true")
        analytic = FaceAnalytic(cascade_path="/nonexistent/cascade.xml")
        assert analytic.available is False
        assert analytic.detect(object()) == []

    def test_a_missing_cascade_says_what_would_fix_it(self, monkeypatch) -> None:
        monkeypatch.setenv("FRS_ENABLED", "true")
        monkeypatch.setattr(
            "services.analytics.frs.FaceAnalytic._find_cascade", staticmethod(lambda: None)
        )
        assert "opencv-data" in (FaceAnalytic().unavailable_reason or "")


class TestWhatItReports:
    def test_a_detection_carries_no_identity(self) -> None:
        """The guard against scope creep, expressed as a test.

        There is no name, no person id, no gallery reference and no match score
        on this record, and adding one is not a small change — it needs a lawful
        authorisation regime, per-query case binding and an audit entry per
        query. A test that names the absent fields makes adding one a deliberate
        act rather than an incidental commit.
        """
        detection = FaceDetection(x=1, y=2, width=3, height=4, level_weight=6.1)
        fields = set(vars(detection))
        assert fields == {"x", "y", "width", "height", "level_weight"}
        for forbidden in ("name", "person_id", "identity", "gallery", "match", "score"):
            assert forbidden not in fields

    def test_the_weight_is_not_squeezed_into_zero_to_one(self) -> None:
        """A 0–1 number beside a face reads as "probability this is a person",
        and there is no identity here to be probable about. The first version
        did normalise, and every detection came back at exactly 1.00."""
        assert FaceDetection(x=0, y=0, width=1, height=1, level_weight=6.37).level_weight > 1


class TestCascadeDiscovery:
    def test_candidates_are_absolute_and_named_for_frontal_faces(self) -> None:
        assert CASCADE_CANDIDATES
        for path in CASCADE_CANDIDATES:
            assert path.startswith("/")
            assert "frontalface" in path
