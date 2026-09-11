"""ncu_runner / attribution 解析器测试（样例文本 fixture，不依赖 GPU/ncu）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_EXPERIMENTS_DIR = _TESTS_DIR.parent
for _p in (str(_TESTS_DIR), str(_EXPERIMENTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# attribution.parse_compute_apps_csv 复用 worker 的纯函数
_WORKER_DIR = _EXPERIMENTS_DIR.parent / "worker"
if str(_WORKER_DIR) not in sys.path:
    sys.path.insert(0, str(_WORKER_DIR))

from depin_experiments import attribution, ncu_runner  # noqa: E402

FIXTURES = _TESTS_DIR / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestQueryMetricsParsing(unittest.TestCase):
    def setUp(self):
        self.names = ncu_runner.extract_metric_names(_fixture("ncu_query_metrics.txt"))

    def test_wanted_metrics_found(self):
        selection = ncu_runner.select_metrics(self.names)
        for expected in (
            "sm__inst_executed_pipe_fp64",
            "sm__inst_executed_pipe_fp32",
            "sm__inst_executed_pipe_alu",
            "sm__pipe_tensor_op_hmma_cycles_active",
            "sm__inst_executed_pipe_tensor_op_hmma",
            "sm__inst_executed_pipe_tensor_op_imma",
        ):
            self.assertIn(expected, selection["found"], f"{expected} 应从 fixture 中命中")

    def test_missing_recorded(self):
        selection = ncu_runner.select_metrics(self.names)
        # fixture 里特意不放 fp16 pipe 指标
        self.assertEqual(selection["missing"], ["sm__inst_executed_pipe_fp16"])

    def test_not_metric_tokens_excluded(self):
        # 单段标识符（无 __）不应被当成指标
        self.assertNotIn("python", self.names)
        self.assertNotIn("cycle", self.names)
        self.assertIn("derived__memory_throughput", self.names)

    def test_empty_and_none(self):
        self.assertEqual(ncu_runner.extract_metric_names(""), [])
        self.assertEqual(ncu_runner.select_metrics([])["found"], [])
        self.assertEqual(len(ncu_runner.select_metrics([])["missing"]), len(ncu_runner.WANTED_METRICS))

    def test_profile_metric_names_suffix(self):
        suffixed = ncu_runner.profile_metric_names(["sm__cycles_elapsed", "gpu__time_duration.sum"])
        self.assertEqual(suffixed, ["sm__cycles_elapsed.sum", "gpu__time_duration.sum"])


class TestProfileCsvParsing(unittest.TestCase):
    def setUp(self):
        parsed = ncu_runner.parse_profile_csv(_fixture("ncu_profile.csv"))
        self.parsed = parsed
        self.rows = parsed["rows"]

    def test_header_located(self):
        self.assertIn("Metric Name", self.parsed["columns"])
        self.assertIn("Metric Value", self.parsed["columns"])
        self.assertEqual(len(self.rows), 8)

    def test_sum_metric_with_comma_thousands(self):
        total = ncu_runner.sum_metric(self.rows, "gpu__time_duration.sum")
        self.assertEqual(total["total"], 1234 + 987654 + 222333 + 3333)
        self.assertEqual(total["kernel_count"], 4)
        self.assertEqual(total["unparsable"], 0)

    def test_sum_metric_skips_na(self):
        total = ncu_runner.sum_metric(self.rows, "sm__inst_executed_pipe_fp64.sum")
        self.assertIsNone(total["total"])
        self.assertEqual(total["unparsable"], 1)

    def test_hmma_totals(self):
        totals = ncu_runner.metric_totals(self.rows)
        self.assertEqual(totals["sm__inst_executed_pipe_tensor_op_hmma.sum"]["total"], 12345 + 4444)
        self.assertEqual(totals["sm__inst_executed_pipe_fp32.sum"]["total"], 567890)

    def test_pids_and_kernels(self):
        self.assertEqual(ncu_runner.extract_pids(self.rows), ["83155"])
        kernels = ncu_runner.extract_kernel_names(self.rows)
        self.assertEqual(len(kernels), 4)
        self.assertTrue(any("gemm" in k for k in kernels))
        self.assertTrue(any("elementwise" in k for k in kernels))

    def test_classify_kernels(self):
        summary = ncu_runner.classify_kernels(ncu_runner.extract_kernel_names(self.rows))
        self.assertEqual(summary["kernel_count"], 4)
        self.assertIn("gemm", summary["by_op_family"])
        self.assertIn("elementwise", summary["by_op_family"])
        self.assertTrue(summary["heuristic"])
        # bf16 kernel 应给出 bf16 域线索
        bf16_kernels = summary["by_math_domain_hint"].get("bf16(tensor)", [])
        self.assertTrue(any("bf16" in k for k in bf16_kernels))

    def test_empty_and_banner_only(self):
        self.assertEqual(ncu_runner.parse_profile_csv("")["rows"], [])
        banner_only = ncu_runner.parse_profile_csv("==PROF== Connected to process 1\nno header here\n")
        self.assertEqual(banner_only["rows"], [])
        self.assertEqual(len(banner_only["banner_lines"]), 2)

    def test_build_profile_command(self):
        command = ncu_runner.build_profile_command(
            "/usr/bin/ncu",
            ["a.sum", "b.sum"],
            ["python", "-m", "mod"],
            replay_mode="application",
            log_file="/tmp/x.csv",
            target_processes="all",
        )
        self.assertEqual(command[0], "/usr/bin/ncu")
        self.assertIn("--replay-mode", command)
        self.assertEqual(command[command.index("--replay-mode") + 1], "application")
        self.assertIn("a.sum,b.sum", command)
        self.assertEqual(command[-3:], ["python", "-m", "mod"])


class TestComputeAppsParsing(unittest.TestCase):
    def test_fixture(self):
        parsed = attribution.parse_compute_apps_csv(_fixture("nvidia_smi_compute_apps.csv"))
        self.assertEqual(len(parsed["rows"]), 2)
        self.assertEqual(parsed["rows"][0]["pid"], 83155)
        self.assertEqual(parsed["rows"][0]["process_name"], "python")
        self.assertEqual(parsed["rows"][0]["used_gpu_memory_bytes"], 512 * 1024 * 1024)
        self.assertEqual(parsed["rows"][1]["used_gpu_memory_bytes"], 256 * 1024 * 1024)

    def test_empty_and_noise(self):
        parsed = attribution.parse_compute_apps_csv("")
        self.assertEqual(parsed["rows"], [])
        parsed = attribution.parse_compute_apps_csv("No running processes found\n")
        self.assertEqual(parsed["rows"], [])
        parsed = attribution.parse_compute_apps_csv("[N/A]\n")
        self.assertEqual(parsed["rows"], [])
        self.assertEqual(parsed["unparsed"], ["[N/A]"])

    def test_header_table_ignored(self):
        # 非 csv 的表格式输出（我们查询用 csv,noheader）：表头行被忽略，
        # 数据行无逗号无法解析 → 诚实进入 unparsed，而不是猜
        table = (
            "Processes:\n"
            "    PID    Process name     GPU Memory\n"
            "===========================================================\n"
            "   83155  python                  512 MiB\n"
        )
        parsed = attribution.parse_compute_apps_csv(table)
        self.assertEqual(parsed["rows"], [])
        self.assertTrue(any("83155" in line for line in parsed["unparsed"]))

    def test_na_memory(self):
        parsed = attribution.parse_compute_apps_csv("123, python, [N/A]\n")
        self.assertEqual(parsed["rows"][0]["used_gpu_memory_bytes"], None)
        self.assertEqual(parsed["rows"][0]["pid"], 123)


if __name__ == "__main__":
    unittest.main()
