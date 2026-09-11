"""报告汇总逻辑测试：三态聚合、信任假设、markdown 渲染（合成输入，无 GPU）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_EXPERIMENTS_DIR = _TESTS_DIR.parent
for _p in (str(_TESTS_DIR), str(_EXPERIMENTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from depin_experiments import report  # noqa: E402
from depin_experiments.common import (  # noqa: E402
    STATUS_BLOCKED,
    STATUS_NOT_COVERED,
    STATUS_PASS,
)


def _envelope(status, reason="r", data=None):
    return {"status": status, "reason": reason, "data": data or {}, "artifacts": {}}


ALL_PASS = {letter: _envelope(STATUS_PASS) for letter in "abcdef"}


class TestItemStatus(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(report.item_status_from_sources([]), STATUS_NOT_COVERED)

    def test_none_is_not_covered(self):
        self.assertEqual(report.item_status_from_sources([None, None]), STATUS_NOT_COVERED)

    def test_all_pass(self):
        self.assertEqual(
            report.item_status_from_sources([STATUS_PASS, STATUS_PASS]), STATUS_PASS
        )

    def test_blocked_wins(self):
        self.assertEqual(
            report.item_status_from_sources([STATUS_PASS, STATUS_BLOCKED]), STATUS_BLOCKED
        )

    def test_not_covered(self):
        self.assertEqual(
            report.item_status_from_sources([STATUS_PASS, STATUS_NOT_COVERED]), STATUS_NOT_COVERED
        )
        self.assertEqual(
            report.item_status_from_sources([STATUS_PASS, None]), STATUS_NOT_COVERED
        )


class TestBuildAssessment(unittest.TestCase):
    def test_all_pass(self):
        assessment = report.build_assessment(ALL_PASS)
        self.assertEqual(len(assessment), 7)
        for item in assessment:
            self.assertEqual(item["status"], STATUS_PASS)

    def test_missing_experiment_is_not_covered(self):
        assessment = report.build_assessment({})
        for item in assessment:
            if item["number"] == 7:
                self.assertEqual(item["status"], STATUS_PASS)  # 报告本身生成
            else:
                self.assertEqual(item["status"], STATUS_NOT_COVERED)

    def test_c_blocked_blocks_items_2_3(self):
        results = dict(ALL_PASS)
        results["c"] = _envelope(STATUS_BLOCKED, "ncu 权限被拒")
        assessment = {item["number"]: item for item in report.build_assessment(results)}
        self.assertEqual(assessment[2]["status"], STATUS_BLOCKED)
        self.assertEqual(assessment[3]["status"], STATUS_BLOCKED)  # c 受阻压过 d 通过
        # 第 5 项来源是 d+e，与 c 无关：不受影响
        self.assertEqual(assessment[5]["status"], STATUS_PASS)
        self.assertEqual(assessment[1]["status"], STATUS_PASS)  # a 不受影响

    def test_e_blocked_blocks_items_4_5(self):
        results = dict(ALL_PASS)
        results["e"] = _envelope(STATUS_BLOCKED, "application replay 失败")
        assessment = {item["number"]: item for item in report.build_assessment(results)}
        self.assertEqual(assessment[4]["status"], STATUS_BLOCKED)
        self.assertEqual(assessment[5]["status"], STATUS_BLOCKED)
        self.assertEqual(assessment[2]["status"], STATUS_PASS)

    def test_partial_sources(self):
        # c 通过、d 未运行 → 第 3 项未覆盖
        results = dict(ALL_PASS)
        results.pop("d")
        assessment = {item["number"]: item for item in report.build_assessment(results)}
        self.assertEqual(assessment[3]["status"], STATUS_NOT_COVERED)
        self.assertEqual(assessment[5]["status"], STATUS_NOT_COVERED)

    def test_notes_reference_reasons(self):
        results = {"a": _envelope(STATUS_BLOCKED, "ncu 缺失")}
        assessment = {item["number"]: item for item in report.build_assessment(results)}
        self.assertIn("ncu 缺失", " ".join(assessment[1]["notes"]))


class TestTrustAssumptions(unittest.TestCase):
    def test_all_pass_still_lists_fundamental_limits(self):
        bullets = report.build_trust_assumptions(ALL_PASS)
        joined = " ".join(bullets)
        self.assertIn("重放", joined)  # e 通过且开销>1 时提示（此处 e 无数据则不强制）
        self.assertIn("无法从这些文件本身证明", joined)  # 恒真信任边界
        self.assertIn("单机单卡", joined)

    def test_ncu_missing_bullet(self):
        results = dict(ALL_PASS)
        results["c"] = _envelope(
            STATUS_BLOCKED,
            "ncu 不可用",
            data={"ncu": {"found": False}},
        )
        bullets = " ".join(report.build_trust_assumptions(results))
        self.assertIn("ncu 不可用", bullets)
        self.assertIn("不足以支撑", bullets)

    def test_permission_denied_bullet(self):
        results = dict(ALL_PASS)
        results["c"] = _envelope(
            STATUS_BLOCKED,
            "权限被拒",
            data={"ncu": {"found": True, "counter_permission": {"verdict": "denied"}}},
        )
        bullets = " ".join(report.build_trust_assumptions(results))
        self.assertIn("ERR_NVGPUCTRPERM", bullets)

    def test_nvml_per_process_unavailable_bullet(self):
        results = dict(ALL_PASS)
        results["d"] = _envelope(
            STATUS_PASS,
            "ok",
            data={"concurrent_attribution": {"nvml_per_process_available": False}},
        )
        bullets = " ".join(report.build_trust_assumptions(results))
        self.assertIn("NVML 每进程 API 不可用", bullets)

    def test_replay_overhead_bullet(self):
        results = dict(ALL_PASS)
        results["e"] = _envelope(
            STATUS_PASS,
            "ok",
            data={"overhead": {"kernel_replay_vs_baseline": 8.2}},
        )
        bullets = " ".join(report.build_trust_assumptions(results))
        self.assertIn("8.2", bullets)

    def test_stop_latency_bullet(self):
        results = dict(ALL_PASS)
        results["f"] = _envelope(
            STATUS_PASS,
            "ok",
            data={"intervals": {"1s": {"idle_observation_latency": {"median": 1.4}}}},
        )
        bullets = " ".join(report.build_trust_assumptions(results))
        self.assertIn("1.40s", bullets)


class TestRenderMarkdown(unittest.TestCase):
    def test_contains_summary_and_sections(self):
        results = dict(ALL_PASS)
        results["c"] = _envelope(
            STATUS_BLOCKED, "ncu 权限被拒", data={"ncu": {"found": True}}
        )
        assessment = report.build_assessment(results)
        trust = report.build_trust_assumptions(results)
        markdown = report.render_markdown(
            results, assessment, trust, meta={"generated_at": "2026-09-11T00:00:00Z", "platform": "test"}
        )
        self.assertIn("GPU 计量可行性实验报告", markdown)
        self.assertIn("| 2 |", markdown)
        self.assertIn(STATUS_BLOCKED, markdown)
        self.assertIn("### c counter_collection", markdown)
        self.assertIn("信任假设", markdown)
        self.assertIn("docs/stage1/gpu-metering-report.md", markdown)

    def test_missing_experiments_rendered_as_not_covered(self):
        markdown = report.render_markdown({}, report.build_assessment({}), report.build_trust_assumptions({}))
        self.assertIn(f"### d attribution_test：{STATUS_NOT_COVERED}", markdown)


class TestIdleObservationLatency(unittest.TestCase):
    """stop_latency 的纯函数部分。"""

    def test_picks_first_idle_after_stop(self):
        from depin_experiments.stop_latency import idle_observation_latency

        samples = [
            {"ts": 100.0, "processes": [{"pid": 1}], "device": {"gpu_util_percent": 90}},
            {"ts": 100.5, "processes": [], "device": {"gpu_util_percent": 0}},
            {"ts": 101.0, "processes": [], "device": {"gpu_util_percent": 0}},
        ]
        result = idle_observation_latency(samples, t_stop=100.2)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["latency_seconds"], 0.3)
        self.assertEqual(result["method"], "compute-processes-empty")

    def test_ignores_idle_before_stop(self):
        from depin_experiments.stop_latency import idle_observation_latency

        samples = [
            {"ts": 100.0, "processes": [], "device": {"gpu_util_percent": 0}},  # 停止前空闲
            {"ts": 100.6, "processes": [{"pid": 1}], "device": {"gpu_util_percent": 80}},
            {"ts": 101.2, "processes": [], "device": {"gpu_util_percent": 0}},
        ]
        result = idle_observation_latency(samples, t_stop=100.3)
        self.assertAlmostEqual(result["latency_seconds"], 101.2 - 100.3)

    def test_util_fallback_method(self):
        from depin_experiments.stop_latency import idle_observation_latency

        samples = [
            {"ts": 100.4, "processes": None, "device": {"gpu_util_percent": 0}},
        ]
        result = idle_observation_latency(samples, t_stop=100.0)
        self.assertEqual(result["method"], "gpu-util-zero")

    def test_no_idle_returns_none(self):
        from depin_experiments.stop_latency import idle_observation_latency

        samples = [{"ts": 100.5, "processes": [{"pid": 9}], "device": {"gpu_util_percent": 50}}]
        self.assertIsNone(idle_observation_latency(samples, t_stop=100.0))
        self.assertIsNone(idle_observation_latency([], t_stop=100.0))


class TestReplayPureFunctions(unittest.TestCase):
    def test_compute_overhead(self):
        from depin_experiments.replay import compute_overhead, compare_metric_totals

        self.assertAlmostEqual(compute_overhead(20.0, 5.0), 4.0)
        self.assertIsNone(compute_overhead(10.0, 0))
        self.assertIsNone(compute_overhead(None, 5.0))

        totals_a = {"m.sum": {"total": 100.0}}
        totals_b = {"m.sum": {"total": 150.0}}
        comparison = compare_metric_totals(totals_a, totals_b)
        self.assertAlmostEqual(comparison["m.sum"]["rel_diff"], 0.5)
        # 无交集
        self.assertEqual(compare_metric_totals(totals_a, {"x.sum": {"total": 1}}), {})


if __name__ == "__main__":
    unittest.main()
