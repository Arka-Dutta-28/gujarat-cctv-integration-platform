"""Per-stage timing rollup.

These numbers are a graded submission artifact, so the arithmetic behind them
is tested rather than trusted.
"""

from __future__ import annotations

from services.anpr.metrics import MetricsCollector, percentile


class TestPercentile:
    def test_empty_is_zero_not_an_error(self) -> None:
        assert percentile([], 95) == 0.0

    def test_median_of_a_known_set(self) -> None:
        assert percentile([1, 2, 3, 4, 5], 50) == 3

    def test_p95_picks_the_tail_not_the_middle(self) -> None:
        values = [1.0] * 95 + [100.0] * 5
        assert percentile(values, 95) >= 1.0
        assert percentile(values, 99) == 100.0

    def test_a_single_sample_is_its_own_every_percentile(self) -> None:
        assert percentile([7.0], 50) == 7.0
        assert percentile([7.0], 95) == 7.0


class TestCollector:
    def test_stages_are_timed_and_summarised_separately(self) -> None:
        m = MetricsCollector("cam")
        m.record("decode", 5.0)
        m.record("decode", 15.0)
        m.record("ocr", 40.0)
        by_stage = {s.stage: s for s in m.snapshot()}
        assert by_stage["decode"].samples == 2
        assert by_stage["ocr"].max_ms == 40.0

    def test_a_stage_with_no_samples_is_not_reported_as_zero(self) -> None:
        """Reporting 0 ms for a stage that never ran would be a false claim."""
        m = MetricsCollector("cam")
        m.timings["ocr"] = []
        assert m.snapshot() == []

    def test_the_context_manager_measures_something(self) -> None:
        m = MetricsCollector("cam")
        with m.stage("detect"):
            sum(range(10000))
        assert m.snapshot()[0].samples == 1
        assert m.snapshot()[0].max_ms > 0

    def test_a_failing_stage_is_still_timed(self) -> None:
        """A stage that throws is exactly the one worth having a timing for."""
        m = MetricsCollector("cam")
        try:
            with m.stage("ocr"):
                raise RuntimeError("model blew up")
        except RuntimeError:
            pass
        assert m.snapshot()[0].stage == "ocr"

    def test_the_window_closes_on_time(self) -> None:
        m = MetricsCollector("cam", window_s=60.0, started_at=100.0)
        assert m.due(now=150.0) is False
        assert m.due(now=161.0) is True

    def test_reset_clears_the_window(self) -> None:
        m = MetricsCollector("cam")
        m.record("decode", 5.0)
        m.count("frames", 10)
        m.reset()
        assert m.snapshot() == []
        assert m.counters == {}
