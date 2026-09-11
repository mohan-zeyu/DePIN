"""实验 e：replay_overhead —— ncu 重放模式的开销对比。

对比三种运行方式的墙钟时间与计数：
1. baseline：无 ncu 直接跑负载；
2. kernel replay（ncu 默认）：每个 kernel 多次重放以分指标组采集；
3. application replay：整个应用重跑若干遍。

记录重放开销倍数，以及两种模式计数差异（spec §7.3 第 4 项：测量开销、
是否需要重放、如何区分业务执行与测量引入的额外执行）。
"""

from __future__ import annotations

import statistics
import sys
from typing import Any

from . import common, ncu_runner
from .common import STATUS_BLOCKED, STATUS_PASS, ResultContext

REPLAY_MODES = ("kernel", "application")


def compute_overhead(profiled_seconds: float, baseline_seconds: float) -> float | None:
    """重放开销倍数（纯函数）。"""
    if not baseline_seconds or baseline_seconds <= 0 or profiled_seconds is None:
        return None
    return profiled_seconds / baseline_seconds


def compare_metric_totals(
    totals_a: dict[str, dict[str, Any]],
    totals_b: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """两种重放模式同名指标合计对比（纯函数）。

    返回每个指标的 {a, b, rel_diff}；rel_diff = (b-a)/a（a 为 kernel 模式）。
    """
    comparison: dict[str, Any] = {}
    for name in sorted(set(totals_a) & set(totals_b)):
        a = totals_a[name].get("total")
        b = totals_b[name].get("total")
        rel = None
        if a not in (None, 0) and b is not None:
            rel = (b - a) / a
        comparison[name] = {"kernel_replay_total": a, "application_replay_total": b, "rel_diff": rel}
    return comparison


def run(out: ResultContext, opts: common.ExperimentOptions) -> dict[str, Any]:
    data: dict[str, Any] = {"replay_batches": opts.replay_batches}

    target_cmd = [
        sys.executable,
        "-m",
        "depin_experiments.workload_entry",
        "--mode",
        "infer",
        "--infer-batches",
        str(opts.replay_batches),
        "--precision",
        "fp32",
        "--seed",
        str(opts.seed),
    ]

    # ---- baseline（3 次取中位数）----------------------------------------
    baseline_runs = []
    for rep in range(3):
        run = common.run_command(
            target_cmd,
            timeout=opts.subprocess_timeout,
            cwd=common.DEPIN_EXPERIMENTS_DIR,
        )
        baseline_runs.append(
            {
                "rep": rep,
                "returncode": run["returncode"],
                "seconds": run["duration_seconds"],
            }
        )
    ok_baseline = [r["seconds"] for r in baseline_runs if r["returncode"] == 0]
    baseline_median = statistics.median(ok_baseline) if ok_baseline else None
    data["baseline"] = {
        "runs": baseline_runs,
        "median_seconds": baseline_median,
    }

    # ---- ncu 前置 --------------------------------------------------------
    ncu_path, ncu_evidence = ncu_runner.find_ncu()
    if ncu_path is None:
        data["ncu"] = {"found": False, "discovery_evidence": ncu_evidence}
        out.write_json("e_replay_overhead.json", data)
        out.register("summary", out.path("e_replay_overhead.json"))
        return {
            "status": STATUS_BLOCKED,
            "reason": "ncu 不可用，重放实验无法执行（baseline 已记录）",
            "data": data,
            "artifacts": dict(out.artifacts),
        }

    permission = ncu_runner.probe_counter_permission(
        ncu_path, timeout=min(opts.subprocess_timeout, 300.0)
    )
    if permission["verdict"] != "granted":
        data["ncu"] = {"found": True, "path": ncu_path, "counter_permission": permission}
        out.write_json("e_replay_overhead.json", data)
        out.register("summary", out.path("e_replay_overhead.json"))
        return {
            "status": STATUS_BLOCKED,
            "reason": f"计数权限受阻（{permission['verdict']}），重放实验无法执行",
            "data": data,
            "artifacts": dict(out.artifacts),
        }

    query = ncu_runner.run_query_metrics(ncu_path, timeout=min(opts.subprocess_timeout, 300.0))
    available = ncu_runner.extract_metric_names(query["stdout"] or "")
    selection = ncu_runner.select_metrics(available, ncu_runner.WANTED_METRICS)
    metrics = ncu_runner.profile_metric_names(selection["found"]) or ["gpu__time_duration.sum"]
    data["ncu"] = {
        "found": True,
        "path": ncu_path,
        "selection": selection,
        "profile_metrics": metrics,
    }
    out.write_text(
        "e_ncu_query_metrics.txt",
        f"rc={query['returncode']}\n\n{query['stdout']}",
    )
    out.register("ncu_query_metrics", out.path("e_ncu_query_metrics.txt"))

    # ---- 两种重放模式 ----------------------------------------------------
    modes: dict[str, Any] = {}
    for mode in REPLAY_MODES:
        csv_path = out.path(f"e_ncu_{mode}_replay.csv")
        # application replay 会整应用重放多遍，放宽超时
        timeout = opts.subprocess_timeout * (3 if mode == "application" else 1)
        profile = ncu_runner.run_profile(
            ncu_path,
            metrics,
            target_cmd,
            log_file=csv_path,
            replay_mode=mode,
            timeout=timeout,
            target_processes="all",
            cwd=common.DEPIN_EXPERIMENTS_DIR,
        )
        rows = profile["parsed"]["rows"]
        out.register(f"ncu_{mode}_replay_csv", csv_path)
        modes[mode] = {
            "returncode": profile["run"]["returncode"],
            "timed_out": profile["run"]["timed_out"],
            "wall_seconds": profile["run"]["duration_seconds"],
            "metric_totals": ncu_runner.metric_totals(rows),
            "kernel_count": len({r.get("Kernel Name") for r in rows}),
            "stderr_tail": common.tail(profile["run"]["stderr"], 800),
        }
        if profile["run"]["returncode"] != 0 and not rows:
            modes[mode]["failed"] = True
    data["modes"] = modes

    kernel_mode = modes.get("kernel", {})
    app_mode = modes.get("application", {})
    data["overhead"] = {
        "kernel_replay_vs_baseline": compute_overhead(
            kernel_mode.get("wall_seconds"), baseline_median
        ),
        "application_replay_vs_baseline": compute_overhead(
            app_mode.get("wall_seconds"), baseline_median
        ),
        "application_vs_kernel": compute_overhead(
            app_mode.get("wall_seconds"), kernel_mode.get("wall_seconds")
        ),
        "metric_comparison": compare_metric_totals(
            kernel_mode.get("metric_totals", {}), app_mode.get("metric_totals", {})
        ),
        "note": (
            "重放（尤其 application replay）会引入额外执行；计费口径必须排除测量"
            "引起的重复执行（spec §7.3-4），本实验只量化开销，不决定计费规则"
        ),
    }

    kernel_ok = kernel_mode.get("returncode") == 0 and kernel_mode.get("kernel_count")
    app_attempted_ran = not app_mode.get("failed") and app_mode.get("kernel_count")
    if kernel_ok:
        reason = "kernel replay 完成并产出计数；application replay " + (
            "完成" if app_attempted_ran else "失败/受阻（已如实记录）"
        )
        status = STATUS_PASS
    else:
        status = STATUS_BLOCKED
        reason = "kernel replay 未产出结果（详见 modes.kernel.stderr_tail）"

    out.write_json("e_replay_overhead.json", data)
    out.register("summary", out.path("e_replay_overhead.json"))
    return {"status": status, "reason": reason, "data": data, "artifacts": dict(out.artifacts)}
