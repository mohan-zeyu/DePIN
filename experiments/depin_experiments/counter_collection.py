"""实验 c：counter_collection —— 硬件计数采集。

对应 spec §7.3 第 2/3 项的“工具、原始指标、单位、覆盖范围”部分：
1. `ncu --query-metrics` 先探测可用指标（重点 fp64/fp32/alu pipe 与
   tensor 相关指标，实际名称以 query 结果为准、原样记录）；
2. 用 ncu 对真实负载子进程采样，保存原始 CSV；
3. torch.profiler(with_flops=True) 另记一份，明确标注为 shape 推导估算
   而非硬件实测（spec §7.1）。
"""

from __future__ import annotations

from typing import Any

from . import common, ncu_runner, workloads
from .common import STATUS_BLOCKED, STATUS_PASS, ResultContext


def run(out: ResultContext, opts: common.ExperimentOptions) -> dict[str, Any]:
    data: dict[str, Any] = {}

    # torch 前置检查（给出明确报错而不是假装成功）
    try:
        workloads._torch()
    except RuntimeError as exc:
        return {
            "status": STATUS_BLOCKED,
            "reason": f"torch 不可用：{exc}",
            "data": {},
            "artifacts": {},
        }
    try:
        support = workloads.hardware_precision_support(opts.device)
    except RuntimeError as exc:
        return {
            "status": STATUS_BLOCKED,
            "reason": f"CUDA 不可用：{exc}",
            "data": {},
            "artifacts": {},
        }
    data["precision_support"] = support

    # ---- ncu 部分 -------------------------------------------------------
    ncu_path, ncu_evidence = ncu_runner.find_ncu()
    ncu_section: dict[str, Any] = {
        "found": ncu_path is not None,
        "path": ncu_path,
        "discovery_evidence": ncu_evidence,
        "wanted_metrics": list(ncu_runner.WANTED_METRICS),
    }
    data["ncu"] = ncu_section

    ncu_ok = False
    if ncu_path is None:
        ncu_section["blocked_reason"] = "ncu 不可用（见 discovery_evidence）"
    else:
        permission = ncu_runner.probe_counter_permission(
            ncu_path, timeout=min(opts.subprocess_timeout, 300.0)
        )
        ncu_section["counter_permission"] = permission
        if permission["verdict"] != "granted":
            ncu_section["blocked_reason"] = (
                f"计数权限受阻（verdict={permission['verdict']}，证据见 counter_permission.evidence）"
            )
        else:
            query = ncu_runner.run_query_metrics(ncu_path, timeout=min(opts.subprocess_timeout, 300.0))
            out.write_text(
                "c_ncu_query_metrics.txt",
                f"$ ncu --query-metrics\nrc={query['returncode']}\n\n{query['stdout']}\n\n[stderr]\n{query['stderr']}",
            )
            out.register("ncu_query_metrics", out.path("c_ncu_query_metrics.txt"))
            available = ncu_runner.extract_metric_names(query["stdout"] or "")
            selection = ncu_runner.select_metrics(available, ncu_runner.WANTED_METRICS)
            ncu_section["available_metric_count"] = len(available)
            ncu_section["selection"] = selection
            if not selection["found"]:
                ncu_section["blocked_reason"] = "query 通过但未匹配到任何目标指标（结果原样存档）"
            else:
                metrics = ncu_runner.profile_metric_names(selection["found"])
                ncu_section["profile_metrics"] = metrics
                profiles: dict[str, Any] = {}
                # 对 fp32 与（若可用）fp16 各采一份；bf16 仅在支持时追加
                target_precisions = [p for p in ("fp32", "fp16") if p in opts.precisions]
                if "bf16" in opts.precisions and support["bf16"]:
                    target_precisions.append("bf16")
                for precision in target_precisions:
                    import sys

                    target_cmd = [
                        sys.executable,
                        "-m",
                        "depin_experiments.workload_entry",
                        "--mode",
                        "infer",
                        "--infer-batches",
                        str(opts.counter_batches),
                        "--precision",
                        precision,
                        "--seed",
                        str(opts.seed),
                    ]
                    csv_path = out.path(f"c_ncu_profile_{precision}.csv")
                    profile = ncu_runner.run_profile(
                        ncu_path,
                        metrics,
                        target_cmd,
                        log_file=csv_path,
                        replay_mode="kernel",
                        timeout=opts.subprocess_timeout,
                        target_processes="all",
                        cwd=common.DEPIN_EXPERIMENTS_DIR,
                        env=None,
                    )
                    out.register(f"ncu_profile_{precision}", csv_path)
                    rows = profile["parsed"]["rows"]
                    kernels = ncu_runner.extract_kernel_names(rows)
                    profiles[precision] = {
                        "returncode": profile["run"]["returncode"],
                        "timed_out": profile["run"]["timed_out"],
                        "wall_seconds": profile["run"]["duration_seconds"],
                        "metric_totals": ncu_runner.metric_totals(rows),
                        "kernel_classification": ncu_runner.classify_kernels(kernels),
                        "kernels": kernels[:80],
                        "stderr_tail": common.tail(profile["run"]["stderr"], 800),
                    }
                ncu_section["profiles"] = profiles
                ncu_ok = any(
                    p.get("returncode") == 0 and p.get("metric_totals")
                    for p in profiles.values()
                )
                if not ncu_ok:
                    ncu_section["blocked_reason"] = "所有 ncu 采样均未产出指标行（原始输出已存档）"

    # ---- torch.profiler 估算部分（无论 ncu 是否可用都记录，但明确标注）----
    profiler_estimates: dict[str, Any] = {}
    try:
        for precision in [p for p in ("fp32", "fp16") if p in opts.precisions]:
            profiler_estimates[precision] = workloads.run_inference_profiled(
                batches=opts.counter_batches,
                precision=precision,
                batch_size=opts.batch_size,
                seed=opts.seed,
                device=opts.device,
            )
    except RuntimeError as exc:
        profiler_estimates["error"] = str(exc)
    out.write_json("c_torch_profiler_estimate.json", profiler_estimates)
    out.register("torch_profiler_estimate", out.path("c_torch_profiler_estimate.json"))
    data["torch_profiler_estimates"] = {
        "warning": (
            "torch.profiler with_flops=True 的 FLOPs 是 shape 推导估算，"
            "不是硬件实测（spec §7.1）；仅作交叉参考，不得用于计费口径"
        ),
        "per_precision": {
            p: {"total_flops_estimate": r.get("total_flops_estimate")}
            for p, r in profiler_estimates.items()
            if isinstance(r, dict)
        },
    }

    # op/shape 清单（同实验 b 的 manifest，方便单独阅读本实验产物）
    out.write_json(
        "c_op_manifest.json",
        {
            "workload": workloads.WORKLOAD_NAME,
            "ops": workloads.describe_ops(batch_size=opts.batch_size),
        },
    )
    out.register("op_manifest", out.path("c_op_manifest.json"))

    if ncu_ok:
        covered = sorted(ncu_section.get("selection", {}).get("found", []))
        missing = sorted(ncu_section.get("selection", {}).get("missing", []))
        status = STATUS_PASS
        reason = f"ncu 采集成功；命中指标 {len(covered)} 个，缺失 {len(missing)} 个（原样记录）；profiler 估算已单独标注"
    else:
        status = STATUS_BLOCKED
        reason = ncu_section.get("blocked_reason", "ncu 采样未完成")
    out.write_json("c_counter_collection.json", data)
    out.register("summary", out.path("c_counter_collection.json"))
    return {"status": status, "reason": reason, "data": data, "artifacts": dict(out.artifacts)}
