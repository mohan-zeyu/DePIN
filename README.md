# DePIN：基于 Hyperledger Fabric 的 AI 算力共享网络原型

首版目标：用户把 NVIDIA GPU 接入网络，为其他用户执行预置 AI 任务，由 Fabric 链码管理
奖励、预算托管、核验与结算。当前处于**里程碑 1/5**（issue #13：搭建最小系统，验证 GPU
计量并确定执行规则）。完整规格见 [docs/project-spec.md](docs/project-spec.md)。

## 仓库结构

| 目录 | 内容 |
| --- | --- |
| `network/` | Fabric 2.5.10 最小开发网络（1 orderer + 1 peer + cli + CCAAS 链码服务），8 组织身份（UserOrg + 3 核验 + 3 仲裁 + OrdererOrg） |
| `chaincode/depin/` | Go 链码 `depincc`：组织门禁、按 MSP 记票（每 MSP 一票）、GPU 登记去重（owner 证书派生） |
| `backend/` | Go API（fabric-gateway）；身份全部由证书派生，绝不读请求体 userId/owner/orgId |
| `worker/` | Python Worker 骨架（GPU 环境探测 + NVML 采样器） |
| `experiments/` | GPU 计量可行性实验 a–g（对应 spec §7.3 七项要求），原始结果在 `experiments/results/` |
| `docs/stage1/` | 阶段一交付文档：未决参数清单（55 项）、信任边界、决策记录、验收测试与计量实验报告 |
| `.github/workflows/` | 分层 CI：tier1 无 GPU 逻辑测试必过；tier2 Fabric 集成手动触发；tier3 GPU self-hosted 门控 |

## 快速开始（新环境）

前置：Docker（含 compose v2）、Go ≥1.25、Python ≥3.10、curl/tar。GPU 部分需要
NVIDIA GPU + uv（可选，仅实验需要）。

```bash
make bootstrap    # 下载 fabric 2.5.10 二进制与镜像（幂等；缺依赖明确报错）
make up           # 生成身份 → 起网络 → 建通道 → CCAAS 部署链码 → 冒烟（一键，失败即停）
make test         # go 两 module + python 纯逻辑测试（无 GPU 依赖）
make down         # 停止并清理全部生成物
```

链上身份隔离演示（真实 Fabric 网络、不同组织证书）：

```bash
bash docs/stage1/evidence/t2-vote-test.sh
```

GPU 计量实验（在带 NVIDIA GPU 的机器上，uv 管理依赖）：

```bash
cd experiments && uv run python run_experiment.py            # 全量 a–g
uv run python run_experiment.py --only b,d,f                 # 子集；无 GPU 时退出码 2
```

各组件详细说明：[network/README.md](network/README.md)、[worker/README.md](worker/README.md)、
[experiments/README.md](experiments/README.md)。

## 阶段一结论（摘要）

- **已实现并实际验证**：最小网络一键启停；链码身份隔离（越权拒绝、同 MSP 双证书一票、
  分证据计票、幂等，链上实测 8/8）；GPU 真实负载（fp32/fp16/bf16 训练+推理）；进程级
  归因（nvidia-smi）；停止延迟采样；纯逻辑测试 132 个。
- **明确受阻**：ncu 硬件性能计数在 WSL2 上报 `ERR_NVGPUCTRPERM`（驱动参数由 Windows
  宿主管理）——按精度运算计数与重放开销未获得，**未回退时间计费/公式估算**；修复路径
  三选一待确认（Windows 注册表 / Windows 侧 Nsight Compute / 原生 Linux 机器）。
- **仍待确认**：55 项业务参数见 [docs/stage1/undecided-parameters.md](docs/stage1/undecided-parameters.md)，
  四类交付状态汇总见 [docs/stage1/acceptance-tests.md](docs/stage1/acceptance-tests.md)。

> 集中部署仅证明角色与流程隔离，不代表六个核验/仲裁组织为独立管理主体（spec §3.2）；
> 硬件计数受阻期间，本系统不宣称具备真实可信的按运算量计费能力（issue #13 验收标准 5）。
