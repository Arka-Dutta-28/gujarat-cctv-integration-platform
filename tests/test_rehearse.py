"""The demo readiness board.

These test the two checks that were silently wrong, because a readiness board
that reports a problem which is not there is worse than no board: an operator
either waits for nothing or "fixes" something twice. Both bugs had the same
shape — a field name that did not exist, and a time window that did.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from scripts.rehearse import (
    ALERT_FRESH_MINUTES,
    ALERT_PLATE,
    CLIP_CYCLE_MINUTES,
    MAX_SHED_FRACTION,
    PERFORMANCE_WINDOW_MINUTES,
    check_alert,
    check_ocr_keeping_up,
)


def _api(payload: dict, seen: list | None = None):
    """A stand-in for `_get` that records the paths asked for."""
    def get(api: str, path: str, token, **kw):
        if seen is not None:
            seen.append(path)
        return payload
    return get


def alert(minutes_ago: float, plate: str = ALERT_PLATE) -> dict:
    stamp = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {"plate": plate, "tier": "probable", "raised_at": stamp.isoformat()}


class TestAlertFreshness:
    def test_a_recent_alert_is_ready(self, monkeypatch):
        monkeypatch.setattr("scripts.rehearse._get", _api({"alerts": [alert(2)]}))
        shot = check_alert("http://x", None)
        assert shot.ready is True
        assert "min old" in shot.detail

    def test_a_stale_alert_is_not(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.rehearse._get",
            _api({"alerts": [alert(ALERT_FRESH_MINUTES + 5)]}),
        )
        assert check_alert("http://x", None).ready is False

    def test_the_timestamp_field_is_raised_at(self, monkeypatch):
        """The bug this file exists for.

        The check originally looked for `ts` or `created_at`. The API returns
        neither, so the age stayed None, `fresh` stayed False, and every alert
        was reported stale — including one raised three minutes earlier.
        """
        stamp = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        payload = {"alerts": [{"plate": ALERT_PLATE, "tier": "probable",
                               "raised_at": stamp, "ts": None, "created_at": None}]}
        monkeypatch.setattr("scripts.rehearse._get", _api(payload))
        assert check_alert("http://x", None).ready is True

    def test_the_newest_alert_decides(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.rehearse._get",
            _api({"alerts": [alert(90), alert(1), alert(45)]}),
        )
        assert check_alert("http://x", None).ready is True

    def test_other_plates_do_not_count(self, monkeypatch):
        # The payoff shot is the *planted* vehicle. A fresh alert for something
        # else does not mean the segment can be filmed.
        monkeypatch.setattr(
            "scripts.rehearse._get",
            _api({"alerts": [alert(1, plate="GJ01AB1234")]}),
        )
        shot = check_alert("http://x", None)
        assert shot.ready is False and ALERT_PLATE in shot.detail


class TestShedWindow:
    def test_the_board_asks_for_a_recent_window(self, monkeypatch):
        """The other bug.

        `/api/performance` defaults to 15 minutes, so after a configuration
        change the board kept reporting the old regime. Observed: 93.9% shed at
        15 minutes and 0.0% at 5, on the same stack.
        """
        seen: list[str] = []
        monkeypatch.setattr(
            "scripts.rehearse._get",
            _api({"anpr": {"load_shedding": {"ocr_shed_fraction": 0.0, "ocr_shed": 0}}}, seen),
        )
        check_ocr_keeping_up("http://x", None)
        assert seen == [f"/api/performance?minutes={PERFORMANCE_WINDOW_MINUTES}"]

    def test_a_high_shed_rate_is_not_ready(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.rehearse._get",
            _api({"anpr": {"load_shedding": {"ocr_shed_fraction": 0.99, "ocr_shed": 100}}}),
        )
        shot = check_ocr_keeping_up("http://x", None)
        assert shot.ready is False
        # The advice must name both halves; --scale alone starts replicas that
        # claim nothing and sit at 0% CPU.
        assert "ANPR_SHARDS" in shot.advice and "--scale" in shot.advice

    def test_a_low_shed_rate_is_ready(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.rehearse._get",
            _api({"anpr": {"load_shedding": {"ocr_shed_fraction": 0.05, "ocr_shed": 3}}}),
        )
        assert check_ocr_keeping_up("http://x", None).ready is True
        assert 0 < MAX_SHED_FRACTION < 1

    def test_the_overlay_filter_eating_plates_is_not_ready(self, monkeypatch):
        """Measured 7-14 Sep: nothing shed, 1,154 suppressed, 0 plates kept."""
        monkeypatch.setattr(
            "scripts.rehearse._get",
            _api({"plate_reads": 0, "load_shedding": {
                "ocr_shed_fraction": 0.0, "ocr_shed": 0, "overlay_suppressed": 1154}}),
        )
        shot = check_ocr_keeping_up("http://x", None)
        assert shot.ready is False
        assert "overlay" in shot.advice and "shards will not help" in shot.advice

    def test_suppressing_a_clock_while_reading_plates_is_ready(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.rehearse._get",
            _api({"plate_reads": 300, "load_shedding": {
                "ocr_shed_fraction": 0.0, "ocr_shed": 0, "overlay_suppressed": 40}}),
        )
        assert check_ocr_keeping_up("http://x", None).ready is True


def test_the_trace_window_sits_between_traverse_and_cycle():
    # The corridor takes ~19.5 min to traverse and repeats every 25. Narrower
    # than the traverse holds half a journey; wider than the cycle holds one
    # vehicle at both ends of the corridor 25 minutes apart, which the
    # plausibility check correctly calls impossible.
    assert 19.5 < CLIP_CYCLE_MINUTES < 25
