#!/usr/bin/env python3
"""GPU 计量可行性实验编排脚本（issue #13 / spec §7.3）。

用法（在 WSL 目标机上，先运行 setup_wsl.sh）::

    python run_experiment.py                    # 依次运行 a-g
    python run_experiment.py --only a,c         # 子集（字母或全名）
    python run_experiment.py --only a-g --train-steps 50
    python run_experiment.py --list             # 列出实验

实验映射（spec §7.3 七项）：
  a environment_survey   环境与计量权限（第 1 项）
  b workload_train_infer 真实训练+推理（负载基础，第 1/2 项前置）
  c counter_collection   ncu 硬件计数 + profiler 估算对照（第 2/3 项）
  d attribution_test     并发进程归因（第 3/5 项）
  e replay_overhead      重放开销（第 4 项）
  f stop_latency         停止观测延迟（第 4 项/预算保护前置）
  g evidence_report      汇总三态结论报告（第 6/7 项）

产物写入 experiments/results/<UTC 时间戳>/。

GPU 门禁：无可用 NVIDIA GPU 时明确退出码 2（--allow-no-gpu 仅用于把
“受阻”如实记录进结果，不会静默通过任何实验）。
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional

EXPERIMENTS_DIR = Path(__file__).resolve().parent
for _p in (str(EXPERIMENTS_DIR),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from depin_experiments import (  # noqa: E402
    attribution,
    common,
    counter_collection,
    env_survey,
    replay,
    report,
    stop_latency,
    workloads,
)

common.ensure_worker_importable()

#: 实验注册表：字母 → (名称, 运行函数)。顺序即默认执行顺序。
REGISTRY: dict[str, tuple[str, Any]] = {
    "a": ("environment_survey", env_survey.run),
    "b": ("workload_train_infer", workloads.run),
    "c": ("counter_collection", counter_collection.run),
    "d": ("attribution_test", attribution.run),
    "e": ("replay_overhead", replay.run),
    "f": ("stop_latency", stop_latency.run),
    "g": ("evidence_report", report.run),
}
ALL_LETTERS = tuple(REGISTRY)
ALIASES = {
    "b1": "b",
    "workload_train": "b",
    "workload_infer": "b",
    **{name: letter for letter, (name, _fn) in REGISTRY.items()},
}


def resolve_selection(tokens: Optional[list[str]]) -> list[str]:
    """把 --only 的 token（字母/别名/全名）解析成有序字母（纯函数）。"""
    if not tokens:
        return list(ALL_LETTERS)
    selected: list[str] = []
    for raw in tokens:
        for token in str(raw).split(","):
            token = token.strip().lower()
            if not token:
                continue
            if token in REGISTRY:
                letter = token
            else:
                letter = ALIASES.get(token)
            if letter is None:
                valid = ", ".join(ALL_LETTERS)
                raise ValueError(f"未知实验 {token!r}；可选：{valid}（或其全名/b1 别名）")
            if letter not in selected:
                selected.append(letter)
    if not selected:
        raise ValueError("--only 解析结果为空")
    return sorted(selected, key=ALL_LETTERS.index)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GPU 计量可行性实验（spec §7.3）；产物写入 experiments/results/<时间戳>/",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--only", default=None, help="逗号分隔的实验子集（字母 a-g / 全名），默认全部")
    parser.add_argument("--list", action="store_true", help="列出实验后退出")
    parser.add_argument("--results-root", default=str(EXPERIMENTS_DIR / "results"), help="结果根目录")
    parser.add_argument("--allow-no-gpu", action="store_true", help="无 GPU 也继续（实验将如实记录“受阻”，用于环境诊断）")
    # 负载参数
    parser.add_argument("--seed", type=int, default=20260911)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--precisions", default="fp32,fp16,bf16", help="逗号分隔：fp32,fp16,bf16 子集")
    parser.add_argument("--train-steps", type=int, default=200)
    parser.add_argument("--infer-batches", type=int, default=50)
    parser.add_argument("--counter-steps", type=int, default=12, help="ncu 采样下的训练步数")
    parser.add_argument("--counter-batches", type=int, default=8, help="ncu 采样下的推理批数")
    parser.add_argument("--replay-batches", type=int, default=10)
    parser.add_argument("--attribution-train-steps", type=int, default=80)
    parser.add_argument("--attribution-noise-seconds", type=float, default=90.0)
    parser.add_argument("--stop-intervals", default="0.1,1", help="停止延迟实验的采样间隔档（秒，逗号分隔）")
    parser.add_argument("--stop-reps", type=int, default=5)
    parser.add_argument("--stop-warmup-seconds", type=float, default=2.0)
    parser.add_argument("--stop-timeout-seconds", type=float, default=20.0)
    parser.add_argument("--subprocess-timeout", type=float, default=900.0)
    args = parser.parse_args(argv)
    if args.list:
        return args
    # 参数校验（非法即退出码 2，不允许带病运行）
    errors: list[str] = []
    try:
        selection = resolve_selection(_split_csv(args.only))
    except ValueError as exc:
        raise SystemExit(f"参数错误：{exc}") from exc
    try:
        precisions = workloads.validate_precision_list(_split_csv(args.precisions))
    except ValueError as exc:
        errors.append(f"--precisions {exc}")
        precisions = ()
    try:
        stop_intervals = tuple(float(x) for x in _split_csv(args.stop_intervals))
    except ValueError as exc:
        errors.append(f"--stop-intervals 非法：{exc}")
        stop_intervals = ()
    if errors:
        raise SystemExit("参数错误：" + "；".join(errors))

    options = common.ExperimentOptions(
        seed=args.seed,
        batch_size=args.batch_size,
        precisions=precisions,
        train_steps=args.train_steps,
        infer_batches=args.infer_batches,
        counter_steps=args.counter_steps,
        counter_batches=args.counter_batches,
        replay_batches=args.replay_batches,
        attribution_train_steps=args.attribution_train_steps,
        attribution_noise_seconds=args.attribution_noise_seconds,
        stop_intervals=stop_intervals,
        stop_reps=args.stop_reps,
        stop_warmup_seconds=args.stop_warmup_seconds,
        stop_timeout_seconds=args.stop_timeout_seconds,
        subprocess_timeout=args.subprocess_timeout,
    )
    try:
        options.validate()
    except ValueError as exc:
        raise SystemExit(f"参数错误：{exc}") from exc
    args.selection = selection
    args.options = options
    return args


def _split_csv(value: Optional[str]) -> list[str]:
    if value is None:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def run_one(letter: str, name: str, fn, ctx: common.ResultContext, opts: common.ExperimentOptions) -> dict[str, Any]:
    started_at = common.utc_now_iso()
    t0 = time.monotonic()
    print(f"[{letter}] {name} 开始 ...", flush=True)
    try:
        result = fn(ctx, opts)
        if not isinstance(result, dict) or "status" not in result:
            raise RuntimeError(f"实验 {letter} 返回结构非法：{result!r}")
        if result["status"] not in common.ALL_STATUSES:
            result = {
                "status": common.STATUS_BLOCKED,
                "reason": f"非法状态 {result.get('status')!r} 被降级为受阻",
                "data": result,
            }
    except Exception as exc:  # noqa: BLE001 —— 实验失败必须记录受阻而不是中断全部
        traceback_str = traceback.format_exc(limit=10)
        print(traceback_str, flush=True)
        result = {
            "status": common.STATUS_BLOCKED,
            "reason": f"实验异常：{type(exc).__name__}: {exc}",
            "error": traceback_str,
        }
    envelope = {
        "experiment": f"{letter}_{name}",
        "status": result["status"],
        "reason": result.get("reason"),
        "started_at": started_at,
        "finished_at": common.utc_now_iso(),
        "duration_seconds": round(time.monotonic() - t0, 3),
        "artifacts": result.get("artifacts") or {},
        "data": result.get("data") or {},
    }
    if result.get("error"):
        envelope["error"] = result["error"]
    ctx.write_json(f"{letter}_{name}.json", envelope)
    print(f"[{letter}] {name} → {envelope['status']}（{envelope['duration_seconds']}s）：{envelope['reason']}", flush=True)
    return envelope


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if args.list:
        print("实验清单（字母 → 名称）：")
        for letter, (name, _fn) in REGISTRY.items():
            print(f"  {letter}  {name}")
        print("--only 支持字母、全名、b1/workload_train 等别名；默认全部运行。")
        return 0

    print(f"depin GPU 计量实验，选择：{args.selection}", flush=True)

    # ---- GPU 门禁（显式失败，不允许静默通过）----------------------------
    gpu_ok, gpu_details = common.check_gpu_available()
    print(f"GPU 门禁：{'通过' if gpu_ok else '未通过'}；细节={gpu_details}", flush=True)
    if not gpu_ok and not args.allow_no_gpu:
        print(
            "\n[退出码 2] 未检测到可用 NVIDIA GPU。拒绝静默通过：真实计量实验必须"
            "在真实 GPU 上运行（spec §6.1/§7.1 禁止估算冒充实测）。\n"
            "如需在无 GPU 机器上仅做环境诊断，可显式加 --allow-no-gpu，"
            "各实验会如实记录为“受阻”。",
            file=sys.stderr,
        )
        return 2

    results_dir = common.make_results_dir(args.results_root)
    print(f"结果目录：{results_dir}", flush=True)
    ctx = common.ResultContext(results_dir)
    ctx.write_json(
        "run_meta.json",
        {
            "started_at": common.utc_now_iso(),
            "argv": sys.argv,
            "selection": args.selection,
            "options": vars(args.options).copy(),
            "gpu_gate": {"passed": gpu_ok, "details": gpu_details},
            "allow_no_gpu": args.allow_no_gpu,
        },
    )

    envelopes: dict[str, dict[str, Any]] = {}
    for letter in args.selection:
        name, fn = REGISTRY[letter]
        envelopes[letter] = run_one(letter, name, fn, ctx, args.options)

    ctx.write_json("summary.json", envelopes)
    print("\n==== 汇总 ====", flush=True)
    for letter, envelope in envelopes.items():
        print(f"  {letter} {envelope['experiment']}: {envelope['status']} —— {envelope['reason']}", flush=True)
    print(f"\n详见：{results_dir}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
