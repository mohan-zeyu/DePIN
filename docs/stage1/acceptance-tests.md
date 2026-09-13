# issue #13 验收测试映射（stage-1）

本文把 issue #13「必须执行的验收测试」表的五个场景逐项展开为可执行的验证条目。约定：

- 引用的路径（`network/scripts/bootstrap.sh`、`network/scripts/up.sh`、`network/scripts/down.sh`、`chaincode/depin`、`backend`、`worker`、`experiments/run_experiment.py`）是 stage-1 计划路径（decision-log.md B7）；若实现阶段路径调整，先更新本文再执行。
- 「实际结果」与「证据留存」全部为占位【待填：主 agent 执行后填写】——**占位未填时，对应场景一律视为未执行，不得宣称通过**（issue #13：「跳过不算通过」「上面的测试是验收要求，尚未宣称已执行」）。
- 每条填写时必须注明环境（真实 GPU / 无 GPU / mock）与日期，四类测试（真实 GPU 验证、Fabric 集成测试、纯逻辑测试、mock 测试）不得混称（issue #13 关闭条件第 3 条）。

---

## T1 新环境启动

| 项 | 内容 |
| --- | --- |
| 验证目的 | 在新检出的环境里按文档完成依赖准备、最小网络启动、身份初始化与停止；步骤可重复；缺少依赖时**明确报错而不是假成功**（issue #13 表格原文）。 |
| 前置条件 | 新 clone 的仓库、干净的环境（无残留容器/卷）；Docker 可用；GPU 环境不是本项必需（启动网络不依赖 GPU）。 |
| 运行命令 | 1. `bash network/scripts/bootstrap.sh`（拉取 Fabric 二进制与镜像、检查依赖，缺失即非零退出并打印缺什么）<br>2. `bash network/scripts/up.sh`（生成 crypto 材料：六组织身份 + 用户 + Worker 身份；起 orderer/peer；建通道；部署 chaincode/depin）<br>3. `bash network/scripts/down.sh`（停止并清理）<br>4. 再次 `bash network/scripts/up.sh`（验证可重复启动） |
| 预期结果 | 每步退出码与输出符合预期：bootstrap 缺依赖时报错并指出缺什么；up 后 peer/链码就绪、六组织 MSP 身份文件存在且可被链码区分；down 后无残留容器/卷；第二次 up 与第一次行为一致。任何「警告但继续、最终静默失败」都算不通过。 |
| 实际结果 | **通过（2026-09-11，环境：macOS Apple M3 + OrbStack Docker 29.4.0，无 GPU）**。bootstrap 幂等完成（fabric 2.5.10 二进制 + 5 镜像）；`up.sh` 一键全流程退出码 0：cryptogen 生成 8 组织身份（OrdererOrg + 7 应用组织；六核验/仲裁组织各含 Admin+User1 双证书）→ configtxgen → orderer/peer/cli 容器 → Raft leader 选举等待 → 通道 depin-channel 创建并加入 → 链码本机交叉编译（linux/arm64）→ CCAAS 打包/install/depin-cc 服务启动/approve/commit → Init 与 GetPolicy 冒烟通过。`down.sh` 清理彻底（容器/卷/镜像/生成物）。当日累计完成 ≥4 轮完整 down→up 重建，行为一致。注意：脚本含 OrbStack/Docker 29 兼容修复（详见 decision-log.md B12–B15）。 |
| 证据留存 | `docs/stage1/evidence/t1-up-final.log`（最后一轮 up.sh 全输出，EXIT=0） |

## T2 身份隔离

| 项 | 内容 |
| --- | --- |
| 验证目的 | 普通用户不能伪造组织权限；同一组织的多张证书不能凑成多个组织的票（issue #13 表格原文；spec §3.2「不能用同一组织的多个用户或证书凑票」、§10「不能相信请求体里的 userId/owner/orgId」）。 |
| 前置条件 | T1 网络已启动；六组织、普通用户、Worker 身份已初始化。 |
| 运行命令 | `cd backend && go test ./...`（覆盖权限中间件与 Gateway 身份映射测试，具体测试名以实现为准，计划含：伪造 orgId 请求、请求体 userId 与证书身份不一致、普通用户调用核验/仲裁接口被拒）<br>`cd chaincode/depin && go test ./...`（计划含：同组织第二张证书的第二票被拒、跨订单/跨证据版本的投票不能合并、单组织两票不达 3+2 门槛） |
| 预期结果 | 伪造 orgId / 借请求体声明他人身份 → 403 或链码 invoke 失败，且失败原因可读；同一 MSP 的第二张用户证书投票 → 计票仍为该组织 1 票，第二票被明确拒绝；普通用户调用组织专属接口全部被拒。零例外。 |
| 实际结果 | **通过（2026-09-11，环境：真实 Fabric 网络，T1 启动）**。分两层：(1) 纯逻辑测试——链码 go test 11/11（含同组织双证书第二票被拒、跨 evidence 不可合并、owner 证书派生）、backend go test 10/10（伪造 owner 字段被忽略）；(2) 链上实测（不同组织真实证书切换 CORE_PEER_MSPCONFIGPATH/LOCALMSPID 调用）8/8：Verify1Org Admin 投票成功；**同 MSP 第二张证书（User1@verify1）投同键被明确拒绝**（"每个 MSP 只计一票"）；**UserOrg 越权投票被拒**（"仅核验组织可提交核验投票"）；Verify2Org 第二票成功（2/2 达门槛 verdict=approve）；不同 evidenceHash 分开计票（ev-1 两票、ev-1 之外 ev-2 一票互不合并）；同身份重复投票幂等（返回原 txId）；设备登记 owner 由证书派生（签名无 owner 参数）；同 gpuUuid 重复登记被拒。 |
| 证据留存 | `docs/stage1/evidence/t2-vote-test.sh`（可重复执行脚本）与 `t2-vote-test-output.log`（链上调用完整输出，含各拒绝错误消息） |

## T3 硬件实测

| 项 | 内容 |
| --- | --- |
| 验证目的 | 运行真实小型 GPU 任务并保存原始计数；加入无关 GPU 进程后，明确哪些运算可按任务归因、不能覆盖的组合记录为不支持（issue #13 表格原文）。不以理论 FLOPs 或时间代替硬件实测（issue #13 验收标准第 2 条）。 |
| 前置条件 | T1 完成；`experiments/` 环境就绪（torch 安装完成、ncu 可用性已知——若 ncu 不可用，本项如实记为受阻并转入 T4 口径）。 |
| 运行命令 | 1. `uv run experiments/run_experiment.py --only env`（环境与计量权限探测，存档 JSON）<br>2. `uv run experiments/run_experiment.py --only workload`（小 CNN 训练 + 批量推理，保存原始计数文件）<br>3. `uv run experiments/run_experiment.py --only attribution`（先起一个无关 GPU 进程——示例：另一个小推理循环——再跑任务，比较计数归因结果）<br>4. `uv run experiments/run_experiment.py --only pollution`（空操作对照：sleep/空循环不得产生可计费计数；同一负载重复执行两次的计数量级比对） |
| 预期结果 | 每步产出带单位、带环境标记的原始指标文件；能归因的组合（如独占窗口整机归因、ncu 目标进程归因）给出明确结论与误差量级；不能归因的组合（如 WSL 下每进程查询不可用时的进程级归因）在报告中记为「不支持」，**绝不**转为时间计费或公式估算替代。结论写入 gpu-metering-report.md 对应小节。 |
| 实际结果 | **部分通过、计数部分受阻（2026-09-11，环境：WSL2 RTX 4060 Laptop 真实 GPU）**。b 负载实测通过：fp32/fp16/bf16 三精度真实训练（小 CNN，合成数据固定 seed，loss 分别 2.321→1.869 / 2.321→2.253 / 2.320→1.822，均下降）+ 批量推理，原始计数材料存档；d 归因实测通过：nvidia-smi --query-compute-apps 可把负载进程与无关 GPU 干扰进程分开（进程级归因可行），但 WSL 下 NVML 每进程利用率字段不可用（记 null）；c 硬件计数**受阻**：ncu 报 ERR_NVGPUCTRPERM（详见 T4），精度分列计数未获得，**未回退到时间计费或公式估算**——torch.profiler FLOPs 单独存档并显式标注"shape 推导估算，非硬件实测"。 |
| 证据留存 | `experiments/results/20260911T113057Z/`（全部 envelope JSON + REPORT.md + 原始采样 JSONL/文本），要点摘录见 gpu-metering-report.md |

## T4 能力失败

| 项 | 内容 |
| --- | --- |
| 验证目的 | 撤销计量权限、改变精度或启用需要重放的指标时，系统报告缺口和开销，**不静默转成时间计费/公式估算**（issue #13 表格原文；spec §7.1「计量权限缺失或无法归因到订单时，不能悄悄用另一种计费口径替代」）。 |
| 前置条件 | T3 至少 workload 部分已跑通，存在可对照的正常基线。 |
| 运行命令 | 1. 撤销计量权限后重跑：`uv run experiments/run_experiment.py --only env && uv run experiments/run_experiment.py --only workload`（Linux 下以非授权用户运行 ncu / 调整 perf 权限；WSL 下权限模型不同，实际手段以实验记录为准，如实写明做了什么）<br>2. 改变精度组合：`uv run experiments/run_experiment.py --only workload --precisions <受限组合>`（如仅 FP32，验证缺失精度的计数请求如何报告）<br>3. 启用需 kernel replay 的指标集：`uv run experiments/run_experiment.py --only overhead`（记录 replay 引入的额外执行与墙钟开销） |
| 预期结果 | 三种情况下均得到**显式失败/缺口报告**（非零退出码或报告内 `unavailable`/`gap` 标记 + 原因），并附开销数值；任何代码路径不得出现「计数拿不到就按运行时长×利用率折算」的回退。缺口按 spec §7.3 记录为待业务确认的替代方案前提。 |
| 实际结果 | **通过（2026-09-11，环境：WSL2，计量权限天然受限场景）**。实测撤销路径即 WSL 默认态：ncu 2025.1.1.0（redist archive 用户级安装）profile 真实 torch 负载报 `ERR_NVGPUCTRPERM - The user does not have permission to access NVIDIA GPU Performance Counters`（内核驱动参数由 Windows 宿主管理，WSL 内无法常规修改）。实验 a/c/e 全部返回**显式受阻**（envelope status=blocked + 原因），退出路径无任何"计数拿不到→按时间折算"回退；e（重放开销）因此未执行。修复路径已列出待确认：Windows 宿主管理员改 NVreg_RestrictProfilingToAdminUsers、Windows 侧 Nsight Compute 对 WSL target、或原生 Linux GPU 机器（见 undecided-parameters.md M2/R2）。 |
| 证据留存 | `experiments/results/20260911T113057Z/a_environment_survey.json`（含 ncu 路径/版本/权限 verdict=error）与 `c_counter_collection.json`（受阻原因）；手工复现命令输出见 gpu-metering-report.md §1 |

## T5 基础 CI

| 项 | 内容 |
| --- | --- |
| 验证目的 | 无 GPU 环境执行逻辑检查：硬件相关测试明确跳过并标记，**不称为硬件验证通过**（issue #13 表格原文；spec §14.16 测试 mock 与真实验证结果明确分开）。 |
| 前置条件 | 无 NVIDIA GPU / 无 pynvml 可用的环境（CI 容器或另一台机器）。 |
| 运行命令 | 1. `cd worker && uv run pytest`（fake NVML 注入的纯逻辑测试；真机测试自动 skip）<br>2. `cd backend && go test ./...`<br>3. `cd chaincode/depin && go test ./...`<br>4. `uv run experiments/run_experiment.py --only env`（应报告无 GPU/无 pynvml 的明确错误或 skip，而非崩溃或假成功） |
| 预期结果 | 逻辑测试通过；硬件相关用例以 skip 标记出现在汇总里（pytest `SKIPPED`、非静默消失），skip 数量与原因可查；任何输出不得把跳过计为「硬件验证通过」。 |
| 实际结果 | **通过（2026-09-11，环境：macOS 无 GPU）**。`worker` unittest 47/47、`experiments` unittest 85/85 全过（fake NVML 注入，无真机依赖）；`run_experiment.py` 在无 GPU 环境运行实测退出码 2（打印"GPU 门禁"检查细节后明确失败，非静默、非假成功）；go test 两 module 通过。GitHub Actions workflow（tier1 必过 / tier2 docker 手动触发 / tier3 self-hosted GPU 由 vars.GPU_RUNNER_ENABLED 门控，缺失显示 skipped）已配置，但**尚未在 GitHub 上实际触发运行**——CI 本身归入"已实现但未验证"。 |
| 证据留存 | 本机测试输出见本文各条；CI 定义 `.github/workflows/ci.yml`（未触发记录） |

---

## 四类交付状态汇总（模板）

stage-1 收尾时按 spec §14 与 issue #13 关闭条件，用下表汇报。填写规则：

- 每个交付条目归入且仅归入一类；
- 「已实现并实际验证」必须附最短复现命令与输出/报告路径（引用上文 T1–T5 的证据）；
- 「已实现但未验证」写明缺什么验证（如：未在真实 GPU 上运行）；
- 「明确受阻」写明阻塞条件与影响（如：WSL 下某计量能力缺失）；
- 「仍待用户确认」逐条指向 `undecided-parameters.md` 的编号，不得用示例参数冒充已批准。

| 类别 | 条目 | 证据/复现命令或未决编号 | 说明 |
| --- | --- | --- | --- |
| 已实现并实际验证 | 最小 Fabric 网络一键启停（8 组织身份、单 orderer、单 peer、通道、CCAAS 链码部署） | `network/scripts/down.sh && network/scripts/up.sh`（EXIT=0），证据 t1-up-final.log | 2026-09-11 于 macOS+OrbStack 完整重建 ≥4 轮 |
| 已实现并实际验证 | 链码身份隔离与门禁（越权拒绝、同 MSP 双证书一票、分 evidence 计票、幂等、GPU 登记 owner 证书派生与去重） | `bash docs/stage1/evidence/t2-vote-test.sh`，证据 t2-vote-test-output.log；`cd chaincode/depin && go test ./...`（11 用例） | 真实 Fabric 网络 + 六组织真实证书实测 8/8 |
| 已实现并实际验证 | 真实 GPU 负载（fp32/fp16/bf16 训练+批量推理，loss 下降） | `experiments/run_experiment.py --only b`（wsl），结果目录 20260911T113057Z | RTX 4060 Laptop，torch 2.14.0+cu130 |
| 已实现并实际验证 | 进程级归因（nvidia-smi query-compute-apps 区分负载与干扰进程） | `experiments/run_experiment.py --only d`，d_attribution_test.json | WSL 下每进程利用率字段不可用已如实记录 |
| 已实现并实际验证 | 停止延迟采样（0.1s/1s 两档多轮重复） | `experiments/run_experiment.py --only f`，f_stop_latency.json + 原始 JSONL | 仅进程退出信号可用（每进程字段 WSL 缺失） |
| 已实现并实际验证 | 无 GPU 纯逻辑测试（worker 47 + experiments 85 + go 两 module） | `make test` / 各目录 unittest discover | GPU 门禁无卡时退出码 2，不假成功 |
| 已实现但未验证 | backend HTTP API 连真实网络（fabric-gateway 集成运行） | `cd backend && go test ./...`（mock 单测 10/10 过；gateway 连网运行未执行） | 缺一次对运行中 peer 的 whoami/devices 实调 |
| 已实现但未验证 | GitHub Actions 分层 CI | `.github/workflows/ci.yml` | 配置就绪，未在 GitHub 实际触发 |
| 明确受阻（计费已不依赖，D1） | ncu 硬件性能计数（ERR_NVGPUCTRPERM；WSL 与 AutoDL 容器均实测受阻，宿主内核参数不可控） | `experiments/results/20260911T113057Z/a_environment_survey.json` | 精度分列硬件计数、重放开销未获得；**2026-09-13 用户决策（D1）改按模板工作单元计费，本项不再阻塞计费**，保留为执行真实性研究；如未来需要可走 Windows 注册表 / 非容器 GPU VM |
| 明确受阻 | 每进程 GPU 利用率/显存 NVML 字段（WSL 驱动不支持） | worker/depin_worker/nvml_sampler.py 输出 null 字段 | 影响 M2 归因方案候选 b 的评估，需原生 Linux 复测 |
| 仍待用户确认 | 全部 55 项业务/经济/治理参数 | `undecided-parameters.md`（重点：R2 计量权限门槛、M1 采信证据等级、M2 订单归因、M4 WSL 是否算可计量环境、E1–E5 经济参数） | 示例配置均非已批准规则 |

初始状态声明：~~截至本文创建时，以上五项验收测试均未执行~~ **更新（2026-09-11）：T1/T2/T5 通过，T3 部分通过（计数受阻如实记录），T4 通过（显式受阻报告，无口径回退）。四类汇总见上表；明确受阻项未以任何模拟结果宣称完成（issue #13 关闭条件）。**
