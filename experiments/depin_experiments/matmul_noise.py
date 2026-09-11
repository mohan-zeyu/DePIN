"""干扰进程：独立 GPU matmul 循环（归因实验 d 用）。

刻意与主负载不同的 kernel 形态（纯 matmul，无 conv），
便于检验计量工具能否把两个进程分开。

用法::

    python -m depin_experiments.matmul_noise --seconds 60 --size 2048 --seed 7
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立 GPU matmul 干扰负载")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--size", type=int, default=2048)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--sync-every", type=int, default=64)
    args = parser.parse_args(argv)

    stopped = {"reason": "completed"}

    def _on_term(signum, frame):
        stopped["reason"] = f"terminated by signal {signum}"
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _on_term)
    try:
        signal.signal(signal.SIGINT, _on_term)
    except ValueError:
        pass

    import torch

    if not torch.cuda.is_available():
        print("ERROR: matmul_noise 需要 CUDA GPU", file=sys.stderr, flush=True)
        print("RESULT_JSON=" + json.dumps({"status": "error", "reason": "no cuda"}), flush=True)
        return 3

    torch.manual_seed(args.seed)
    device = "cuda"
    a = torch.randn(args.size, args.size, device=device)
    b = torch.randn(args.size, args.size, device=device)

    iterations = 0
    checksum = 0.0
    deadline = time.monotonic() + args.seconds
    t0 = time.perf_counter()
    try:
        while time.monotonic() < deadline:
            c = a @ b
            iterations += 1
            if iterations % args.sync_every == 0:
                torch.cuda.synchronize()
                checksum += float(c[0, 0].item())
    except SystemExit:
        pass
    finally:
        try:
            torch.cuda.synchronize()
        except Exception:
            pass
    elapsed = time.perf_counter() - t0
    print(
        "RESULT_JSON="
        + json.dumps(
            {
                "status": "ok",
                "reason": stopped["reason"],
                "iterations": iterations,
                "matrix_size": args.size,
                "seconds": elapsed,
                "matmuls_per_second": iterations / elapsed if elapsed > 0 else None,
                "sample_checksum": checksum,
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
