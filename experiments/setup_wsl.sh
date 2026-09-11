#!/usr/bin/env bash
# WSL2 Ubuntu 上搭建 GPU 计量实验环境（stage-1）。
#
# 约束与决策：
# - 系统 Python 3.14 缺 python3-venv/ensurepip，不能标准 venv → 用 uv；
# - 依赖管理走 uv 项目模式（本目录 pyproject.toml + uv.lock + .venv）；
# - 镜像默认用清华 PyPI（可用 UV_DEFAULT_INDEX 覆盖）；
# - nvidia-nsight-compute（ncu 的 pip wheel）装不上时如实失败（退出码 1），
#   不静默跳过——届时 c/e/d 实验的 ncu 部分会记录“受阻”。
#
# 用法（WSL 目标机上，任意目录）：
#   bash experiments/setup_wsl.sh
# 可用环境变量：
#   PYTHON_VERSION   默认 3.12（torch 轮子覆盖最稳；本机系统 3.14 缺 venv）
#   UV_BIN           显式指定 uv 路径（默认 PATH → ~/.local/bin/uv）
#   UV_DEFAULT_INDEX 默认 https://pypi.tuna.tsinghua.edu.cn/simple

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"

# ---- 1. 定位 uv（非交互 ssh 下常不在 PATH）--------------------------------
UV_BIN="${UV_BIN:-}"
if [ -z "$UV_BIN" ]; then
  if command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
  elif [ -x "$HOME/.local/bin/uv" ]; then
    UV_BIN="$HOME/.local/bin/uv"
  else
    echo "ERROR: 未找到 uv（PATH 与 ~/.local/bin/uv 均无）。" >&2
    echo "  安装：curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
  fi
fi
echo "==> uv: $UV_BIN"
echo "==> 镜像: $UV_DEFAULT_INDEX"

# ---- 2. 项目 pyproject.toml（幂等：不存在才创建最小骨架）--------------------
if [ ! -f pyproject.toml ]; then
  cat > pyproject.toml <<'EOF'
[project]
name = "depin-experiments"
version = "0.1.0"
description = "GPU 计量可行性实验（stage-1，spec §7.3）"
requires-python = ">=3.10"
EOF
  echo "==> 已创建 pyproject.toml"
fi

# ---- 3. 创建 .venv（uv 管理的 Python，绕开系统 3.14 缺 ensurepip）----------
echo "==> 创建 .venv（Python ${PYTHON_VERSION}）"
"$UV_BIN" venv --python "$PYTHON_VERSION"

# ---- 4. 主依赖（必须成功）---------------------------------------------------
echo "==> uv add torch / pynvml"
"$UV_BIN" add "torch>=2.4" "pynvml>=11.5"

# ---- 5. ncu（允许失败，但必须如实报告）--------------------------------------
NCU_FAILED=0
echo "==> uv add nvidia-nsight-compute（ncu CLI wheel）"
if ! "$UV_BIN" add nvidia-nsight-compute; then
  NCU_FAILED=1
  echo "" >&2
  echo "=================================================================" >&2
  echo "ERROR: nvidia-nsight-compute 安装失败（当前镜像/平台无对应 wheel）。" >&2
  echo "  影响：counter_collection / replay_overhead / attribution 的 ncu" >&2
  echo "  部分将如实记录为「受阻」；torch.profiler 估算部分不受影响。" >&2
  echo "  可选替代：从 NVIDIA 官网安装 Nsight Compute 后把 ncu 放进 PATH，" >&2
  echo "  实验脚本会自动发现（PATH / venv bin / 常见安装路径）。" >&2
  echo "=================================================================" >&2
fi

# ---- 6. 自检 ----------------------------------------------------------------
PY="$SCRIPT_DIR/.venv/bin/python"
echo "==> 环境自检"
"$PY" - <<'EOF'
import sys
print("python:", sys.version.split()[0])
try:
    import torch
    print("torch:", torch.__version__, "cuda_runtime:", torch.version.cuda)
    print("cuda_available:", torch.cuda.is_available())
    if torch.cuda.is_available():
        print("device:", torch.cuda.get_device_name(0))
except Exception as exc:
    print("torch 不可用:", exc)
    sys.exit(1)
try:
    import pynvml
    print("pynvml:", getattr(pynvml, "__version__", "unknown"))
except Exception as exc:
    print("pynvml 不可用:", exc)
    sys.exit(1)
EOF
NCU_BIN=""
if [ -x "$SCRIPT_DIR/.venv/bin/ncu" ]; then
  NCU_BIN="$SCRIPT_DIR/.venv/bin/ncu"
  "$NCU_BIN" --version | head -2 || true
fi

echo ""
echo "==> 完成。运行实验："
echo "    cd $SCRIPT_DIR"
echo "    uv run python run_experiment.py            # 全部 a-g"
echo "    uv run python run_experiment.py --only a   # 先做环境调查"
if [ "$NCU_FAILED" -ne 0 ]; then
  echo ""
  echo "WARNING: ncu 安装失败（上文已说明影响），setup 以失败状态退出。" >&2
  exit 1
fi
