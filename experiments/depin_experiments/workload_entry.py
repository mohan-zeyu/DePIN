"""实验负载子进程入口：被 ncu 采样 / 归因实验以独立进程方式运行。

用法::

    python -m depin_experiments.workload_entry --mode both \
        --train-steps 20 --infer-batches 8 --precision fp32 --seed 20260911

最后一行输出 ``RESULT_JSON={...}``，父进程用 common.parse_result_json 解析。
"""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="小型真实 CNN 工作负载（训练/推理）")
    parser.add_argument("--mode", choices=["train", "infer", "both"], default="both")
    parser.add_argument("--train-steps", type=int, default=20)
    parser.add_argument("--infer-batches", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--precision", default="fp32", choices=["fp32", "fp16", "bf16"])
    parser.add_argument("--seed", type=int, default=20260911)
    parser.add_argument("--lr", type=float, default=0.05)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args(argv)

    from . import workloads

    result: dict = {
        "workload": workloads.WORKLOAD_NAME,
        "mode": args.mode,
        "precision": args.precision,
        "seed": args.seed,
    }
    if args.mode in ("train", "both"):
        result["train"] = workloads.run_training(
            steps=args.train_steps,
            precision=args.precision,
            batch_size=args.batch_size,
            seed=args.seed,
            lr=args.lr,
            device=args.device,
        )
    if args.mode in ("infer", "both"):
        result["inference"] = workloads.run_inference(
            batches=args.infer_batches,
            precision=args.precision,
            batch_size=args.batch_size,
            seed=args.seed,
            device=args.device,
        )
    print("RESULT_JSON=" + json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
