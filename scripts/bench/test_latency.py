"""Tests for the percentile/summary math in latency.py (issues #31/#55).

Stdlib-only (unittest), no repo test-runner wiring needed: `python3 scripts/bench/test_latency.py`
or `python3 -m unittest scripts.bench.test_latency` from the repo root.
"""

from __future__ import annotations

import unittest

from latency import percentile, summarize


class PercentileTests(unittest.TestCase):
    def test_single_value(self) -> None:
        self.assertEqual(percentile([42.0], 50), 42.0)
        self.assertEqual(percentile([42.0], 99), 42.0)

    def test_p50_is_median_for_odd_length(self) -> None:
        self.assertEqual(percentile([1.0, 2.0, 3.0], 50), 2.0)

    def test_p0_and_p100_are_min_and_max(self) -> None:
        values = [5.0, 1.0, 9.0, 3.0]
        self.assertEqual(percentile(values, 0), 1.0)
        self.assertEqual(percentile(values, 100), 9.0)

    def test_linear_interpolation_between_ranks(self) -> None:
        # 5 values, ranks 0..4: p50 -> rank 2.0 -> value at index 2.
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertEqual(percentile(values, 50), 30.0)
        # p75 -> rank 3.0 exactly (interpolation fraction 0) -> 40.0
        self.assertEqual(percentile(values, 75), 40.0)
        # p10 -> rank 0.4 -> interpolate between values[0]=10 and values[1]=20
        self.assertAlmostEqual(percentile(values, 10), 14.0)

    def test_unsorted_input_is_sorted_first(self) -> None:
        self.assertEqual(percentile([3.0, 1.0, 2.0], 50), 2.0)

    def test_known_p99_matches_hand_computed_value(self) -> None:
        # 100 values 1..100: p99 rank = 0.99 * 99 = 98.01 -> between index 98 (value 99) and
        # index 99 (value 100), fraction 0.01.
        values = [float(i) for i in range(1, 101)]
        self.assertAlmostEqual(percentile(values, 99), 99.01, places=6)

    def test_empty_list_raises(self) -> None:
        with self.assertRaises(ValueError):
            percentile([], 50)

    def test_out_of_range_p_raises(self) -> None:
        with self.assertRaises(ValueError):
            percentile([1.0, 2.0], -1)
        with self.assertRaises(ValueError):
            percentile([1.0, 2.0], 100.1)


class SummarizeTests(unittest.TestCase):
    def test_summary_fields_and_throughput(self) -> None:
        # 10 identical 2ms decisions: mean/p50/p95/p99/max all 2.0, throughput = 1000/2 = 500/s.
        values = [2.0] * 10
        s = summarize(values)
        self.assertEqual(s.n, 10)
        self.assertAlmostEqual(s.mean_ms, 2.0)
        self.assertAlmostEqual(s.p50_ms, 2.0)
        self.assertAlmostEqual(s.p99_ms, 2.0)
        self.assertAlmostEqual(s.max_ms, 2.0)
        self.assertAlmostEqual(s.throughput_per_sec, 500.0)

    def test_max_reflects_worst_case_not_just_p99(self) -> None:
        values = [1.0] * 99 + [1000.0]
        s = summarize(values)
        self.assertEqual(s.max_ms, 1000.0)
        # p99 with linear interpolation at n=100 sits at rank 0.99*99=98.01, between the 99th
        # 1.0 and the single 1000.0 outlier -- close to but not equal to the true max.
        self.assertLess(s.p99_ms, s.max_ms)
        self.assertGreater(s.p99_ms, s.p50_ms)


if __name__ == "__main__":
    unittest.main()
