"""CSV import: header aliasing, type coercion and adapter inference.

These are the pure parts of the import path, tested without a database. The
all-or-nothing transaction behaviour is exercised against the live API.
"""

from __future__ import annotations

import pytest

from services.api.routers.imports import _coerce, _infer_adapter, _normalise_header


class TestHeaderAliasing:
    """A department's own spreadsheet should import without being reformatted."""

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("Asset ID", "external_ref"),
            ("asset_id", "external_ref"),
            ("Site", "name"),
            ("Camera Name", "name"),
            ("RTSP URL", "stream_ref"),
            ("stream_url", "stream_ref"),
            ("Latitude", "lat"),
            ("Longitude", "lon"),
            ("lng", "lon"),
            ("FOV", "fov_degrees"),
            ("Range", "range_m"),
            ("Dept", "department"),
            ("Type", "kind"),
        ],
    )
    def test_common_spellings_map_to_canonical_fields(self, given: str, expected: str) -> None:
        assert _normalise_header(given) == expected

    def test_canonical_names_pass_through(self) -> None:
        assert _normalise_header("bearing") == "bearing"

    def test_case_and_separators_are_ignored(self) -> None:
        assert _normalise_header("  CAMERA-NAME  ") == "name"


class TestAdapterInference:
    """Departments list URLs, not adapter names — 'adapter' is our word."""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("rtsp://10.0.0.5:554/stream1", "rtsp"),
            ("RTSPS://10.0.0.5:322/s", "rtsp"),
            ("https://live.example.in/stream/7", "http"),
            ("http://cam.example.in/feed", "http"),
            ("file:///data/clip.mp4", "file"),
        ],
    )
    def test_scheme_determines_adapter(self, url: str, expected: str) -> None:
        assert _infer_adapter(url) == expected

    def test_m3u8_over_http_is_hls_not_plain_http(self) -> None:
        """HLS needs a playlist reader, not a byte-range fetch."""
        assert _infer_adapter("https://example.in/live/index.m3u8") == "hls"

    def test_unknown_scheme_infers_nothing(self) -> None:
        assert _infer_adapter("srt://example.in:9000") is None
        assert _infer_adapter("just-a-path") is None


class TestCoercion:
    def test_numeric_columns_become_numbers(self) -> None:
        out = _coerce({"lat": "23.05", "lon": "72.52", "bearing": "90", "range_m": "70"})
        assert out == {"lat": 23.05, "lon": 72.52, "bearing": 90, "range_m": 70}

    def test_integer_fields_are_not_left_as_floats(self) -> None:
        """`bearing` is a SMALLINT; handing psycopg 90.0 is a type mismatch."""
        out = _coerce({"bearing": "90.0", "fov_degrees": "60.0"})
        assert out["bearing"] == 90
        assert isinstance(out["bearing"], int)
        assert isinstance(out["fov_degrees"], int)

    def test_blank_means_not_supplied_rather_than_null(self) -> None:
        """An empty spreadsheet cell must not overwrite a good value."""
        out = _coerce({"name": "Cam", "bearing": "", "district": "   "})
        assert out == {"name": "Cam"}

    def test_whitespace_is_stripped(self) -> None:
        assert _coerce({"name": "  Vastrapur  "}) == {"name": "Vastrapur"}

    def test_bad_number_raises_for_the_caller_to_report(self) -> None:
        with pytest.raises(ValueError):
            _coerce({"lat": "not-a-latitude"})
