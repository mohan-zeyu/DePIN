"""common 纯逻辑测试：统计、RESULT_JSON 解析、选项校验。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_EXPERIMENTS_DIR = _TESTS_DIR.parent
for _p in (str(_TESTS_DIR), str(_EXPERIMENTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from depin_experiments import common  # noqa: E402


class TestMinMedianMax(unittest.TestCase):
    def test_basic(self):
        stats = common.min_median_max([3, 1, 2])
        self.assertEqual(stats, {"n": 3, "min": 1.0, "median": 2.0, "max": 3.0})

    def test_even_count_median(self):
        stats = common.min_median_max([4, 1, 3, 2])
        self.assertEqual(stats["median"], 2.5)

    def test_none_values_skipped(self):
        stats = common.min_median_max([5, None, 7, None])
        self.assertEqual(stats["n"], 2)
        self.assertEqual(stats["min"], 5.0)
        self.assertEqual(stats["max"], 7.0)

    def test_empty(self):
        self.assertEqual(
            common.min_median_max([]),
            {"n": 0, "min": None, "median": None, "max": None},
        )
        self.assertEqual(
            common.min_median_max([None, None]),
            {"n": 0, "min": None, "median": None, "max": None},
        )


class TestResultJsonParsing(unittest.TestCase):
    def test_parses_last_marker(self):
        stdout = "log line\nRESULT_JSON={\"a\": 1}\nmore log\nRESULT_JSON={\"b\": 2}\n"
        self.assertEqual(common.parse_result_json(stdout), {"b": 2})

    def test_no_marker(self):
        self.assertIsNone(common.parse_result_json("plain output\n"))
        self.assertIsNone(common.parse_result_json(""))

    def test_broken_json(self):
        self.assertIsNone(common.parse_result_json("RESULT_JSON={broken"))

    def test_non_dict_rejected(self):
        self.assertIsNone(common.parse_result_json("RESULT_JSON=[1,2]"))


class TestTail(unittest.TestCase):
    def test_short(self):
        self.assertEqual(common.tail("abc"), "abc")

    def test_long_truncated(self):
        text = "x" * 5000
        result = common.tail(text, limit=100)
        self.assertTrue(result.startswith("..."))
        self.assertEqual(len(result), 103)


class TestExperimentNames(unittest.TestCase):
    def test_registry_names_match_common(self):
        # run_experiment.REGISTRY 与 common.EXPERIMENT_NAMES 必须一致
        sys.path.insert(0, str(_EXPERIMENTS_DIR))
        import run_experiment

        for letter, name in common.EXPERIMENT_NAMES.items():
            self.assertIn(letter, run_experiment.REGISTRY)
            self.assertEqual(run_experiment.REGISTRY[letter][0], name)


class TestExperimentOptions(unittest.TestCase):
    def _options(self, **overrides):
        base = dict(
            stop_intervals=(0.1, 1.0),
            stop_reps=5,
            train_steps=10,
            subprocess_timeout=60.0,
            attribution_noise_seconds=10.0,
        )
        base.update(overrides)
        return common.ExperimentOptions(**base)

    def test_defaults_valid(self):
        common.ExperimentOptions().validate()

    def test_bad_precision(self):
        with self.assertRaises(ValueError):
            self._options(precisions=("fp8",)).validate()

    def test_empty_precisions(self):
        with self.assertRaises(ValueError):
            self._options(precisions=()).validate()

    def test_bad_stop_intervals(self):
        with self.assertRaises(ValueError):
            self._options(stop_intervals=()).validate()
        with self.assertRaises(ValueError):
            self._options(stop_intervals=(0.1, -1)).validate()

    def test_bad_positive_ints(self):
        for field in ("train_steps", "infer_batches", "stop_reps"):
            with self.assertRaises(ValueError):
                self._options(**{field: 0}).validate()

    def test_bad_stop_timeout(self):
        with self.assertRaises(ValueError):
            self._options(stop_timeout_seconds=0).validate()


if __name__ == "__main__":
    unittest.main()
