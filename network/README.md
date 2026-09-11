# depin 开发网络（stage-1 最小拓扑）

基于 Hyperledger Fabric 2.5.10 的最小开发网络：1 个 orderer + 1 个 peer + 1 个 CLI 容器，
用于验证 **身份与流程隔离**（组织门禁、按 MSP 记票、证书派生 owner），不用于性能或多主体治理验证。

## 拓扑

```
                        ┌──────────────────────────────────────────────────┐
                        │                depin-channel                     │
                        └──────────────────────────────────────────────────┘
                          │                                   │
              ┌───────────┴───────────┐            ┌──────────┴──────────┐
              │  orderer.depin.dev    │            │ peer0.user.depin.dev│
              │  OrdererOrg (etcdRaft)│◄──TLS─────►│ UserOrg（唯一 peer） │
              │  :7050  ops :9443     │            │ :7051  ops :9444    │
              └───────────────────────┘            └──────────┬──────────┘
                                                              │
                                                    ┌─────────┴─────────┐
                                                    │  cli (depin-cli)  │
                                                    │  fabric-tools     │
                                                    │  UserOrg Admin    │
                                                    └───────────────────┘

  身份齐备但不运行 peer 的组织（通过 peer0.user.depin.dev 提交交易，
  链码 depincc 内按 MSP 做组织门禁）：

      核验组织  Verify1Org / Verify2Org / Verify3Org   （verify1/2/3.depin.dev）
      仲裁组织  Arbiter1Org / Arbiter2Org / Arbiter3Org（arbiter1/2/3.depin.dev）
      用户组织  UserOrg（user.depin.dev）——普通用户与 Worker 身份都发在这里
```

- 排序：单节点 etcdRaft，`orderer.depin.dev:7050`，MSP `OrdererOrg`，经典系统通道方式
  从 genesis block 启动。
- 通道：`depin-channel`，成员为全部 7 个非排序组织。
- 链码：`depincc`（Go，源码 `../chaincode/depin`），合约命名空间 `depin.DepinContract`。
- Endorsement / LifecycleEndorsement：`OR('UserOrg.peer', 'UserOrg.admin')`
  （单 peer 开发形态；组织角色门禁由链码内部执行，见链码 `SubmitVerificationVote` / `RecordDeviceEnrollment`）。

## 启动 / 停止

前置：Docker（含 compose v2 或 docker-compose v1）、curl、tar。镜像
`hyperledger/fabric-{peer,orderer,tools,ccenv,baseos}:2.5.10` 为官方多架构镜像，
linux/amd64 与 linux/arm64 均可（Docker 按宿主架构自动选择）。

```bash
# 1. 下载 fabric 2.5.10 二进制（darwin-arm64/amd64、linux-arm64/amd64 自动识别）
#    并拉取镜像（幂等，可重复执行）
./network/scripts/bootstrap.sh          # 或在仓库根目录 make bootstrap

# 2. 生成证书/通道工件 → 启动容器 → 创建通道 → 部署链码（package/install/approve/commit）
#    → 调用 Init → GetPolicy 冒烟查询；任一步失败即停
./network/scripts/up.sh                 # 或 make up

# 3. 停止并清理（compose down -v + 删除生成物）
./network/scripts/down.sh               # 或 make down
```

`up.sh` 细节：

- **链码以 CCAAS（chaincode-as-a-service）方式部署**：up.sh 在本机交叉编译链码
  （`GOOS=linux GOARCH=<容器架构> CGO_ENABLED=0 go build`，需本机 Go 工具链），
  打入 `depin-cc:1.0` 镜像并作为独立容器常驻（监听 9999）；链码包为 type=ccaas
  （connection.json 指向 `depin-cc:9999`，`tls_required=false`），peer 经镜像内置
  `ccaas_builder` 直连链码服务。原因：Fabric 2.5 内置 docker client（API 1.25）与
  Docker 29+/OrbStack（最低 API 1.40）不兼容，peer 在 install 时现场构建 golang
  链码镜像会失败（实测无 env 可绕过）。见 docs/stage1/decision-log.md B8。
- 健康等待基于 orderer/peer 的 operations 端口（`/healthz`，9443/9444），超时默认 120s
  （`HEALTH_TIMEOUT_SECS` 可覆盖），超时即报错退出，不假成功。
- 通道创建前会等待 orderer 日志出现 Raft leader 选举完成（单节点选举晚于健康检查，
  立即建通道会 SERVICE_UNAVAILABLE；`peer channel fetch` 探测不可用——UserOrg 身份
  读系统通道返回 FORBIDDEN）。

## 排障记录（2026-09-11 实测沉淀，遇到同类报错先查这里）

| 症状 | 原因与处置 |
| --- | --- |
| `client version 1.25 is too old`（install 链码） | Fabric 2.5 docker client 与 Docker 29+/OrbStack 不兼容 → 已改 CCAAS（见上） |
| `no endpoints currently defined`（peer 加入通道后） | configtx.yaml 的 OrdererOrg 缺 `OrdererEndpoints` → 已加 `orderer.depin.dev:7050` |
| `yaml: map merge requires map or sequence of maps` | Capabilities 锚点值必须 `V2_5: true`（map），不能裸标量参与 `<<:` merge |
| `unknown orderer type: etcdRaft` | 拼写必须全小写 `etcdraft` |
| genesis.block 挂载报 `not a directory` | OrbStack 单文件 bind mount 兼容问题 → 改挂 `./channel-artifacts` 目录 |
| peer 报 `Config File "core" Not Found` | 整目录挂载覆盖了镜像内 core.yaml → 只挂 msp/tls 子目录 |
| orderer `bind: address already in use`（127.0.0.1:9443） | Fabric 2.5 admin server 与 operations 默认同端口 → `ORDERER_ADMIN_LISTENADDRESS=127.0.0.1:9445` |
| 改 compose 挂载后容器行为怪异 | OrbStack compose recreate 不清旧挂载 → `./scripts/down.sh` 全清后再 up |
| CCAAS 链码容器 panic `flag 'peer.address' must be set` | 方向错了：server 模式不需要连 peer；确认 `CHAINCODE_SERVER_ADDRESS` 与 `CORE_CHAINCODE_ID_NAME=<package-id>`（纯 package-id，不带 `:label` 后缀） |
| 调用链码 `timeout expired while starting chaincode` | depin-cc 未启动 / CCID 不匹配 / connection.json 地址不可达，按序排查 |

## 链上写操作注意

`peer chaincode query` 只在 peer 模拟执行、不落账——**登记/投票等写操作必须 invoke**
（`--waitForEvent`），否则状态不持久（曾因此误判重复登记未拒绝）。

## 身份清单（cryptogen 输出，`network/organizations/`）

生成命令：`cryptogen generate --config=crypto-config.yaml --output=organizations`。
`up.sh` 会把 `User3@user.depin.dev` 复制为 `worker1@user.depin.dev`。

| 身份 | MSP | 目录（相对 `organizations/`） | 用途 |
| --- | --- | --- | --- |
| orderer | OrdererOrg | `ordererOrganizations/depin.dev/orderers/orderer.depin.dev` | 排序节点 |
| OrdererOrg Admin | OrdererOrg | `ordererOrganizations/depin.dev/users/Admin@depin.dev` | 排序管理 |
| peer0 | UserOrg | `peerOrganizations/user.depin.dev/peers/peer0.user.depin.dev` | 唯一 peer |
| UserOrg Admin | UserOrg | `peerOrganizations/user.depin.dev/users/Admin@user.depin.dev` | 链码部署（cli 默认身份） |
| user1 | UserOrg | `peerOrganizations/user.depin.dev/users/User1@user.depin.dev` | 普通用户 / 任务发布者 |
| user2 | UserOrg | `peerOrganizations/user.depin.dev/users/User2@user.depin.dev` | 普通用户 / 算力提供者 |
| worker1 | UserOrg | `peerOrganizations/user.depin.dev/users/worker1@user.depin.dev` | Worker 运行身份（由 User3 复制） |
| Verify1Org Admin | Verify1Org | `peerOrganizations/verify1.depin.dev/users/Admin@verify1.depin.dev` | 核验组织 1 |
| Verify1Org User1 | Verify1Org | `peerOrganizations/verify1.depin.dev/users/User1@verify1.depin.dev` | 核验组织 1 第二证书（同 MSP 双证书测试） |
| Verify2Org Admin | Verify2Org | `peerOrganizations/verify2.depin.dev/users/Admin@verify2.depin.dev` | 核验组织 2 |
| Verify2Org User1 | Verify2Org | `peerOrganizations/verify2.depin.dev/users/User1@verify2.depin.dev` | 核验组织 2 第二证书 |
| Verify3Org Admin | Verify3Org | `peerOrganizations/verify3.depin.dev/users/Admin@verify3.depin.dev` | 核验组织 3 |
| Verify3Org User1 | Verify3Org | `peerOrganizations/verify3.depin.dev/users/User1@verify3.depin.dev` | 核验组织 3 第二证书 |
| Arbiter1Org Admin | Arbiter1Org | `peerOrganizations/arbiter1.depin.dev/users/Admin@arbiter1.depin.dev` | 仲裁组织 1 |
| Arbiter1Org User1 | Arbiter1Org | `peerOrganizations/arbiter1.depin.dev/users/User1@arbiter1.depin.dev` | 仲裁组织 1 第二证书 |
| Arbiter2Org Admin | Arbiter2Org | `peerOrganizations/arbiter2.depin.dev/users/Admin@arbiter2.depin.dev` | 仲裁组织 2 |
| Arbiter2Org User1 | Arbiter2Org | `peerOrganizations/arbiter2.depin.dev/users/User1@arbiter2.depin.dev` | 仲裁组织 2 第二证书 |
| Arbiter3Org Admin | Arbiter3Org | `peerOrganizations/arbiter3.depin.dev/users/Admin@arbiter3.depin.dev` | 仲裁组织 3 |
| Arbiter3Org User1 | Arbiter3Org | `peerOrganizations/arbiter3.depin.dev/users/User1@arbiter3.depin.dev` | 仲裁组织 3 第二证书 |

以核验组织身份投票的示例（cli 容器内切换 MSP，同一 peer 提交）：

```bash
docker compose -f network/docker-compose.yaml exec cli bash -c '
  export CORE_PEER_LOCALMSPID=Verify1Org
  export CORE_PEER_MSPCONFIGPATH=/opt/organizations/peerOrganizations/verify1.depin.dev/users/Admin@verify1.depin.dev/msp
  peer chaincode invoke -o orderer.depin.dev:7050 --tls \
    --cafile /opt/organizations/ordererOrganizations/depin.dev/orderers/orderer.depin.dev/tls/ca.crt \
    -C depin-channel -n depincc \
    --peerAddresses peer0.user.depin.dev:7051 \
    --tlsRootCertFiles /opt/organizations/peerOrganizations/user.depin.dev/peers/peer0.user.depin.dev/tls/ca.crt \
    -c "{\"function\":\"depin:SubmitVerificationVote\",\"Args\":[\"order-1\",\"<evidenceHash>\",\"v1\",\"approve\"]}"
'
```

## 集中部署的局限性（重要）

本网络是 **开发环境的集中部署简化**，仅证明“角色与流程隔离”，**不代表真实管理主体独立**
（project-spec §3.2 的要求）：

1. 六个核验/仲裁组织的身份（MSP、Admin 钱包）齐备，链码内按 MSP 做组织门禁、按 MSP 记票；
   但它们 **不各自运行 peer**，账本与排序均由单一组织（UserOrg + OrdererOrg）的节点承载。
2. 因此“三组织各一票、至少两票”的门槛在链码数据层成立，但这些组织并非独立运维主体：
   peer/orderer 的实际控制者可以审查或重组账本数据。集中部署 **不能作为真实独立核验的安全证明**。
3. 单 orderer 的 etcdRaft 无容错；生产形态需要各组织独立运行 peer/orderer 并分开保管 CA/TLS 根。
4. cryptogen 生成的开发证书 **不适用于任何真实环境**（无外部信任根、无成员服务）。

## 生成物与忽略规则

以下目录由脚本生成，不入库（见根目录 `.gitignore`）：

- `network/bin/`：bootstrap 下载的 fabric 二进制与配置。
- `network/organizations/`：cryptogen 输出（全部身份材料）。
- `network/channel-artifacts/`：genesis block、通道交易、链码包。
- `network/build/`：CCAAS 链码交叉编译二进制（up.sh 每次重建）。
