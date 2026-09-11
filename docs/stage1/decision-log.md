# stage-1 决策记录（issue #13）

本文件只**记录**已作出的选择，不新增业务决策。所有条目分三类，与 spec 开头的三分法对应：

- **A 类：沿用的已确认业务规则**——来自用户，stage-1 无权更改；
- **B 类：stage-1 工程选择**——实现者可自行决定的技术路线（spec §10：推荐工程选择，不是已确认业务规则），每条记录理由与后果；
- **C 类：待业务确认事项**——本阶段明确不拍板，汇总指向未决清单。

业务规则一旦被用户确认或修改，更新 A 类或 C 类对应条目并注明日期；工程选择可被后续阶段推翻，推翻时在本文件追加记录而非删除历史。

---

## A. 沿用的已确认业务规则

完整清单见 `docs/project-spec.md` §2 及 `undecided-parameters.md` 末节「已确认不可改」。stage-1 涉及实现或文档时以下列规则为硬约束（此处仅列条目，出处均为 §2）：

1. Hyperledger Fabric，关键业务规则由链码定义并执行。
2. 每张物理 GPU 单独登记；用户 → 集群 → 主机 → GPU 归属关系。
3. 接入奖励额度 = 统一奖励系数 × 标准算力测试得分（系数本身未决）。
4. 奖励按累计合格可服务时间解锁，168 小时全部解锁。
5. 任务按硬件实测 GPU 运算量计费；不按占用时长或理论估算直接收费；公式估算不得标为硬件实测。
6. 不同精度不同权重，统一单价；价格与权重随订单锁定版本。
7. 任务代币全部分配给算力提供者，平台不抽成。
8. 普通用户之间不允许自由转账。
9. 正常核验三组织至少两票；争议由另外三个未参与组织、至少两票裁决；每组织最多一票；投票绑定同一申请/订单与同一证据版本。
10. 发布者 24 小时确认或异议，到期默认同意（条件齐备才起算）。
11. 整单连续执行；失败或预算不足全额退款，提供者本单收入为零；无阶段机制、无运行中人工介入、无自动重试。
12. 发布时一次托管预算上限；不透支、不追加。
13. 只支持平台预置模板任务；必须真实训练或推理。
14. 集群优先；跨集群须发布者启动前同意。
15. 任务收入链上结算后立即可用。
16. §2 的作废方案清单（按人发币、固定报价、阶段付款、分段续费、原组织仲裁等）不得复活。

---

## B. stage-1 工程选择

### B1. 集中式开发部署：单 orderer + 单 peer，六组织仅 MSP 身份

- **选择**：`network/` 使用 1 个 orderer + 1 个 peer 的最小 Fabric 拓扑；用户、Worker、三个正常核验组织、三个仲裁组织的身份通过 crypto 配置齐备（六组织身份 = 六个 MSP 身份），但**没有**各自独立的 peer/orderer 进程。
- **理由**：开发机资源有限（单 GPU 笔记本 + WSL2）；spec §3.2 明确允许「把这些逻辑组织放到同一台开发机上的简化方案」；六组织权限与 3+2/3+2 门槛逻辑在链码与应用层即可完整实现和测试。
- **后果（必须随附声明）**：这是**角色与流程隔离**，不是「真实管理主体独立」。所有「三组织两票」在 stage-1 通过链码身份校验验证的是**规则逻辑**，不是多主体独立背书；任何文档、页面、报告不得把集中部署当作真实独立核验的安全证明（spec §3.2、§7.3；详见 trust-assumptions.md §2.5）。将来迁移到多组织部署时，改动面预计限于 crypto 配置与网络拓扑，业务链码逻辑不变——此预期待迁移时验证。

### B2. Go 链码 + Go 后端 + Python Worker

- **选择**：链码 `chaincode/depin`（Go）；后端 `backend/`（Go，进程内模块化，走 Fabric Gateway）；Worker `worker/`（Python）。
- **理由**：Fabric 链码与 Gateway 的一等公民是 Go；spec §10 建议「一个进程内模块化后端即可，不强制拆微服务」；AI 负载生态在 Python。
- **后果**：双语言栈，链上/链下数据结构（计量材料、心跳证据）需要显式 schema 版本化，避免 Go/Python 各自演化导致解析漂移。前端（Svelte 或更简）延后到里程碑 5，stage-1 不做。

### B3. ncu 作为硬件计数首选来源（状态：待实测确认）

- **选择**：计量口径首选 ncu 硬件性能计数器；NVML 只作可用性/时间线佐证；torch.profiler 只作 shape 推导的交叉核对。
- **理由**：spec §7.1 要求收费依据是硬件实测运算量；三者数据来源不同（硬件计数器 / 驱动自报 / 公式推导，见 trust-assumptions.md §2.1），只有 ncu 可能满足口径。
- **后果**：在 `gpu-metering-report.md` 用实测确认「ncu 在本机（WSL2 + RTX 4060 Laptop）可用、能取到所需指标、权限与开销可接受」之前，本条只是**待验证的工程倾向**；若实测失败，按 undecided-parameters.md M1 的候选方案报告缺口，绝不静默换成时间计费或公式估算。

### B4. 合成数据 + 小 CNN 作为测试负载

- **选择**：stage-1 计量实验用程序化合成数据（固定种子）+ 小型 CNN，同时演示训练与批量推理。
- **理由**：无版权与下载依赖、确定性可控、8GB 显存可跑；spec §6.1 允许提小型图像分类模型作为工程建议。
- **后果**：这是**实验负载**，不是用户已批准的任务模板——实际模型与数据集仍是未决项（undecided-parameters.md T1/T2）；不得借该负载的存在声明模板验收规则或准确率阈值已获同意（spec §9.1）。

### B5. 测试环境隔离 fixture 原则

- **选择**：测试余额通过显式标记的隔离 fixture 注入（如专用 test mint 入口 + 明确命名），可控时钟（注入 clock）加速时间相关测试；正常业务发币路径与测试路径物理分离；无 GPU 环境的测试通过注入 fake（如 `worker/tests/fakes.py` 的 fake NVML）覆盖纯逻辑，硬件相关测试显式 skip 并在输出中标明。
- **理由**：spec §5「不能把任意给账户铸币的开发接口作为正常业务发币入口」；§12「测试中的短钟或虚拟余额不能冒充正常业务参数」；issue #13 基础 CI 行「无 GPU 的测试明确跳过硬件部分，不称为硬件验证通过」。
- **后果**：所有示例配置文件中的数值（单价、权重、奖励系数）仅为「可运行示例」；CI 报告必须区分 passed / skipped，skipped 不得计入硬件验证通过。

### B6. uv 管理 Python 依赖

- **选择**：Python 侧统一用 uv（本机 0.12.11）管理依赖与虚拟环境；`worker/` 保持极轻（运行时仅 pynvml）；torch 等重依赖只放在 `experiments/`，Worker 包导入不依赖 torch。
- **理由**：锁文件明确、安装快（配合镜像源）；worker 与重依赖解耦使无 GPU 的逻辑测试可以在最小环境下运行（与 B5 一致）。
- **后果**：文档与 CI 命令以 uv 为准（`uv run` / `uv sync`）；torch 首次安装经清华镜像进行（完成情况见 gpu-metering-report.md 环境节）。

### B7. 仓库布局

- **选择**：`network/`（Fabric 启动脚本与配置）、`chaincode/depin/`（链码）、`backend/`（Go 后端）、`worker/`（Python Worker）、`experiments/`（计量实验与真实负载）、`docs/`（规格与阶段文档）。
- **理由**：与 issue #13 交付物和验收测试命令引用的路径一致；目录即阶段职责边界。
- **后果**：`acceptance-tests.md` 引用的脚本路径以此为约定；若实现阶段路径调整，须同步更新该文档，避免文档与实际命令漂移。

---

### B8. 链码以 CCAAS（chaincode-as-a-service）方式部署（2026-09-11 实测后确定）
Fabric 2.5.10 内置 docker client（fsouza/go-dockerclient，默认 API 1.25）与 Docker 29+/OrbStack 的构建 API（最低 1.40）不兼容，peer 在 install 时构建 golang 链码镜像必然失败（实测 `client version 1.25 is too old`，DOCKER_API_VERSION env 不被读取）。改用官方 cc_service 方式：链码二进制由 up.sh 本机交叉编译（linux/arm64 或 amd64 按容器平台）→ 打进 `depin-cc:1.0` 镜像常驻运行 → 链码包 type=ccaas（connection.json 指向 depin-cc:9999，tls_required=false）→ peer 经镜像内置 ccaas_builder 按地址直连。后果：链码服务与 peer 生命周期解耦（down.sh 需一并清理）；stage-1 单机部署下这是流程等价替代，不代表生产形态。

### B9. configtx 必含 OrdererOrg.OrdererEndpoints + Capabilities 锚点用 map 值（2026-09-11 修复记录）
实测两处必配：(1) OrdererOrg 缺 `OrdererEndpoints` 时 peer 加入通道后报 "no endpoints currently defined"，lifecycle 交易全部超时；(2) Capabilities 锚点值必须是 `V2_5: true` 形态（map），裸标量 `V2_5` 与 `<<:` merge 组合会 YAML 解析失败；OrdererType 拼写为 `etcdraft`（非 etcdRaft）。已固化在 network/configtx.yaml。

### B10. up.sh 内置 Raft leader 选举等待（2026-09-11）
单节点 etcdRaft 在 operations /healthz 通过后仍需数秒完成选举，立即创建通道会得到 SERVICE_UNAVAILABLE。up.sh 轮询 orderer 日志出现 "Start accepting requests as Raft leader" 再继续；不能用 `peer channel fetch` 探测（cli 的 UserOrg 身份读系统通道返回 FORBIDDEN，非 leader 信号）。

### B11. OrbStack 兼容修正（2026-09-11）
(1) 单文件 bind mount（genesis.block）在 OrbStack 上报 not a directory，改挂目录；(2) compose recreate 不清理旧挂载（曾出现新旧挂载并存），改配置后必须 down 全清再 up；(3) Fabric 2.5 admin server 默认占 127.0.0.1:9443 与 operations 冲突，显式改 9445。三处均已在 docker-compose.yaml 固化，README 排障节有记录。

## C. 待业务确认事项（不在 stage-1 拍板）

stage-1 把所有影响交易双方权利或计费口径的选择集中登记在 `undecided-parameters.md`（55 项，状态全部「未确认」），不在本文重复。按 spec §12 要求，真正阻塞近期实现的少数关键问题集中在：

| 主题 | 未决项编号 | 为什么近期需要 |
| --- | --- | --- |
| 计量采信口径 | M1–M4、O1–O6 | 决定 worker 计量实现与计费链码字段，实验报告完成后即可确认 |
| 经济参数 | E1–E5 | 阻塞订单结算与奖励额度链码的最终定稿（可先用隔离测试值开发，见 B5） |
| 奖励账务 | A1–A4、B1–B4 | 阻塞里程碑 2 的铸造与解锁账本实现 |
| 可服务验证 | S1–S5 | 与 A2 解锁粒度联动，阻塞心跳协议设计 |
| 模板与验收 | T1–T4 | 阻塞里程碑 3 的真实任务闭环 |
| 治理与核验运维 | G1–G5、V1–V6 | 阻塞里程碑 4 的仲裁与超时路径 |

向用户提问时按 spec §12 的方式：一次集中提出少量真正阻塞的项，附可比较方案（方案已在未决清单各行的「候选方案」列）。
