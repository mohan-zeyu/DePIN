"""run_experiment CLI 参数校验测试（不运行实验、不需要 GPU/torch）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_EXPERIMENTS_DIR = _TESTS_DIR.parent
for _p in (str(_TESTS_DIR), str(_EXPERIMENTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_experiment  # noqa: E402
from depin_experiments import common  # noqa: E402


class TestResolveSelection(unittest.TestCase):
    def test_default_all(self):
        self.assertEqual(run_experiment.resolve_selection(None), list("abcdefg"))

    def test_letters_ordered(self):
        self.assertEqual(run_experiment.resolve_selection(["c", "a"]), ["a", "c"])
        self.assertEqual(run_experiment.resolve_selection(["a,c"]), ["a", "c"])

    def test_duplicates_removed(self):
        self.assertEqual(run_experiment.resolve_selection(["a", "a,b"]), ["a", "b"])

    def test_full_names(self):
        self.assertEqual(
            run_experiment.resolve_selection(["environment_survey", "counter_collection"]),
            ["a", "c"],
        )

    def test_aliases(self):
        self.assertEqual(run_experiment.resolve_selection(["b1"]), ["b"])
        self.assertEqual(run_experiment.resolve_selection(["workload_train"]), ["b"])
        self.assertEqual(run_experiment.resolve_selection(["workload_infer"]), ["b"])

    def test_unknown_token_raises(self):
        with self.assertRaises(ValueError):
            run_experiment.resolve_selection(["x"])
        with self.assertRaises(ValueError):
            run_experiment.resolve_selection(["a", "zzz"])

    def test_empty_tokens(self):
        with self.assertRaises(ValueError):
            run_experiment.resolve_selection(["", ","])


class TestParseArgs(unittest.TestCase):
    def test_defaults(self):
        args = run_experiment.parse_args([])
        self.assertEqual(args.selection, list("abcdefg"))
        self.assertEqual(args.options.precisions, ("fp32", "fp16", "bf16"))
        self.assertEqual(args.options.stop_intervals, (0.1, 1.0))
        args.options.validate()

    def test_only_subset(self):
        args = run_experiment.parse_args(["--only", "a,c"])
        self.assertEqual(args.selection, ["a", "c"])

    def test_list_flag(self):
        args = run_experiment.parse_args(["--list"])
        self.assertTrue(args.list)

    def test_precisions_subset(self):
        args = run_experiment.parse_args(["--precisions", "fp32,fp16"])
        self.assertEqual(args.options.precisions, ("fp32", "fp16"))

    def test_invalid_precision_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            run_experiment.parse_args(["--precisions", "fp8"])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_invalid_only_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            run_experiment.parse_args(["--only", "nope"])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_invalid_stop_intervals_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            run_experiment.parse_args(["--stop-intervals", "abc"])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_numeric_validation_exits(self):
        with self.assertRaises(SystemExit):
            run_experiment.parse_args(["--train-steps", "-5"])

    def test_options_flow_through(self):
        args = run_experiment.parse_args(
            ["--train-steps", "7", "--stop-reps", "2", "--stop-intervals", "0.5,2"]
        )
        self.assertEqual(args.options.train_steps, 7)
        self.assertEqual(args.options.stop_reps, 2)
        self.assertEqual(args.options.stop_intervals, (0.5, 2.0))


class TestGpuGate(unittest.TestCase):
    def test_gate_returns_details_even_without_gpu(self):
        # 在无 GPU 测试机上，门禁必须给出可读 details 且不崩溃
        ok, details = common.check_gpu_available()
        self.assertIsInstance(ok, bool)
        self.assertIn("checks", details)


if __name__ == "__main__":
    unittest.main()
