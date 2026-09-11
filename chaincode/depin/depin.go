// depin.go 实现 stage-1 链码 depincc 的合约 depin.DepinContract。
// 本阶段业务最小化，核心是身份隔离与组织门禁：
//   - 核验投票仅 Verify1/2/3Org 可提交，且每个 MSP 只计一票（按 MSPID 记票，
//     同组织第二张证书投同一键直接拒绝，防止用多证书凑票，spec §3.2）。
//   - 投票必须绑定同一 (orderId, evidenceHash, verdictVersion, verdict)，
//     不同 evidenceHash 的投票分开计数，不能拼成多数（spec §3.2）。
//   - 设备登记仅 UserOrg 可提交，owner 一律由调用者证书派生，绝不接受参数传入。
package main

import (
	"encoding/json"
	"fmt"

	"github.com/hyperledger/fabric-chaincode-go/v2/pkg/cid"
	"github.com/hyperledger/fabric-contract-api-go/v2/contractapi"
)

// ---- 固定策略常量（stage-1；经济参数等未决项见 docs/project-spec.md §12）----

const (
	policyVersion         = 1
	verificationThreshold = 2 // 核验：三个核验组织至少 2 个同意
	arbitrationThreshold  = 2 // 仲裁：三个仲裁组织至少 2 个同意
	userOrgMSP            = "UserOrg"
)

var verificationOrgs = []string{"Verify1Org", "Verify2Org", "Verify3Org"}
var arbitrationOrgs = []string{"Arbiter1Org", "Arbiter2Org", "Arbiter3Org"}

// ---- 状态键 ----

const (
	stateKeyNetworkMeta  = "network~meta"
	stateKeyDevicePrefix = "device~" // + gpuUuid
	voteKeyObjectType    = "verifVote"
)

// VoteRecord 是一条核验投票的账本记录。键为复合键
// verifVote~(orderId, evidenceHash, verdictVersion, mspId)：按 MSP 记票。
type VoteRecord struct {
	OrderID        string `json:"orderId"`
	EvidenceHash   string `json:"evidenceHash"`
	VerdictVersion string `json:"verdictVersion"`
	MSPID          string `json:"mspid"`
	Verdict        string `json:"verdict"` // approve | reject
	VoterClientID  string `json:"voterClientId"`
	TxID           string `json:"txId"`
}

// VoteTally 是对同一 (orderId, evidenceHash, verdictVersion) 的计票结果。
// Verdict 为空表示尚未达到门槛；不同 evidenceHash 的计票互不影响。
type VoteTally struct {
	OrderID        string       `json:"orderId"`
	EvidenceHash   string       `json:"evidenceHash"`
	VerdictVersion string       `json:"verdictVersion"`
	Votes          []VoteRecord `json:"votes"`
	ApproveCount   int          `json:"approveCount"`
	RejectCount    int          `json:"rejectCount"`
	Threshold      int          `json:"threshold"`
	Verdict        string       `json:"verdict"` // "" | approve | reject
}

// DeviceRecord 是一张物理 GPU 的接入登记记录。
// Owner 由调用者证书派生（x509 subject DN），链码不接受参数传入的 owner。
type DeviceRecord struct {
	GpuUUID         string `json:"gpuUuid"`
	Owner           string `json:"owner"`
	OwnerMSPID      string `json:"ownerMspid"`
	ScoreReportHash string `json:"scoreReportHash"`
	Status          string `json:"status"`
	EnrollTxID      string `json:"enrollTxId"`
}

// WhoAmIResult 返回调用者链上身份。
type WhoAmIResult struct {
	MSPID    string `json:"mspid"`
	ClientID string `json:"clientId"`
}

// OrgSet 描述一组同职责组织及其门槛。
type OrgSet struct {
	Orgs      []string `json:"orgs"`
	Threshold int      `json:"threshold"`
}

// Policy 是 stage-1 的常量策略。
type Policy struct {
	PolicyVersion int    `json:"policyVersion"`
	Verification  OrgSet `json:"verification"`
	Arbitration   OrgSet `json:"arbitration"`
	SetsDisjoint  bool   `json:"setsDisjoint"`
}

// DepinContract 是链码 depincc 的合约（命名空间 "depin"，见 main.go）。
type DepinContract struct {
	contractapi.Contract
}

// callerIdentity 从交易上下文解析调用者 MSP ID 与证书身份（x509 subject DN）。
// 所有归属/门禁判断只能使用这里的派生值。
func callerIdentity(ctx contractapi.TransactionContextInterface) (mspid, clientID string, err error) {
	stub := ctx.GetStub()
	mspid, err = cid.GetMSPID(stub)
	if err != nil {
		return "", "", fmt.Errorf("无法解析调用者 MSP ID: %w", err)
	}
	cert, err := cid.GetX509Certificate(stub)
	if err != nil {
		return "", "", fmt.Errorf("调用者不是 X.509 身份，无法派生 clientID: %w", err)
	}
	return mspid, cert.Subject.String(), nil
}

// Init 写入网络元数据（policy 版本）。幂等：已存在时不覆盖、不报错。
func (c *DepinContract) Init(ctx contractapi.TransactionContextInterface) error {
	existing, err := ctx.GetStub().GetState(stateKeyNetworkMeta)
	if err != nil {
		return fmt.Errorf("读取网络元数据失败: %w", err)
	}
	if existing != nil {
		return nil
	}
	meta, err := json.Marshal(map[string]int{"policyVersion": policyVersion})
	if err != nil {
		return err
	}
	return ctx.GetStub().PutState(stateKeyNetworkMeta, meta)
}

// WhoAmI 返回调用者 {mspid, clientId}，用于联调时核对身份映射。
func (c *DepinContract) WhoAmI(ctx contractapi.TransactionContextInterface) (*WhoAmIResult, error) {
	mspid, clientID, err := callerIdentity(ctx)
	if err != nil {
		return nil, err
	}
	return &WhoAmIResult{MSPID: mspid, ClientID: clientID}, nil
}

// GetPolicy 返回常量策略：核验与仲裁两组组织各 3 个、门槛各 2，且两组互不重叠。
func (c *DepinContract) GetPolicy(_ contractapi.TransactionContextInterface) (*Policy, error) {
	return &Policy{
		PolicyVersion: policyVersion,
		Verification:  OrgSet{Orgs: verificationOrgs, Threshold: verificationThreshold},
		Arbitration:   OrgSet{Orgs: arbitrationOrgs, Threshold: arbitrationThreshold},
		SetsDisjoint:  true,
	}, nil
}

// SubmitVerificationVote 提交核验投票。仅 Verify1/2/3Org 可调用。
// 记票单位是 MSP 而不是证书：同组织第二张证书对同一键投票直接拒绝；
// 完全相同的重复投票（同证书、同结论）幂等成功且不增加票数。
func (c *DepinContract) SubmitVerificationVote(ctx contractapi.TransactionContextInterface, orderId, evidenceHash, verdictVersion, verdict string) (*VoteRecord, error) {
	if orderId == "" || evidenceHash == "" || verdictVersion == "" {
		return nil, fmt.Errorf("orderId/evidenceHash/verdictVersion 均不能为空")
	}
	if verdict != "approve" && verdict != "reject" {
		return nil, fmt.Errorf("verdict 必须为 approve 或 reject，收到 %q", verdict)
	}

	mspid, clientID, err := callerIdentity(ctx)
	if err != nil {
		return nil, err
	}
	if !contains(verificationOrgs, mspid) {
		return nil, fmt.Errorf("仅核验组织 %v 可提交核验投票，当前调用者 MSP=%s", verificationOrgs, mspid)
	}

	stub := ctx.GetStub()
	key, err := stub.CreateCompositeKey(voteKeyObjectType, []string{orderId, evidenceHash, verdictVersion, mspid})
	if err != nil {
		return nil, fmt.Errorf("构造投票复合键失败: %w", err)
	}

	raw, err := stub.GetState(key)
	if err != nil {
		return nil, fmt.Errorf("读取既有投票失败: %w", err)
	}
	if len(raw) > 0 {
		var prev VoteRecord
		if err := json.Unmarshal(raw, &prev); err != nil {
			return nil, fmt.Errorf("解析既有投票失败: %w", err)
		}
		if prev.VoterClientID != clientID {
			return nil, fmt.Errorf(
				"MSP %s 已由证书 %q 投过票（orderId=%s evidenceHash=%s verdictVersion=%s）：每个 MSP 只计一票，同组织第二张证书不得再投",
				mspid, prev.VoterClientID, orderId, evidenceHash, verdictVersion)
		}
		if prev.Verdict != verdict {
			return nil, fmt.Errorf("MSP %s 已投 %q，不得更改为 %q", mspid, prev.Verdict, verdict)
		}
		return &prev, nil // 幂等：完全相同的重复投票
	}

	rec := &VoteRecord{
		OrderID:        orderId,
		EvidenceHash:   evidenceHash,
		VerdictVersion: verdictVersion,
		MSPID:          mspid,
		Verdict:        verdict,
		VoterClientID:  clientID,
		TxID:           stub.GetTxID(),
	}
	val, err := json.Marshal(rec)
	if err != nil {
		return nil, err
	}
	if err := stub.PutState(key, val); err != nil {
		return nil, fmt.Errorf("写入投票失败: %w", err)
	}
	return rec, nil
}

// GetVerificationVotes 返回同一 (orderId, evidenceHash, verdictVersion) 下的
// 按组织计票结果。不同 evidenceHash / verdictVersion 的投票分开计数。
func (c *DepinContract) GetVerificationVotes(ctx contractapi.TransactionContextInterface, orderId, evidenceHash, verdictVersion string) (*VoteTally, error) {
	if orderId == "" || evidenceHash == "" || verdictVersion == "" {
		return nil, fmt.Errorf("orderId/evidenceHash/verdictVersion 均不能为空")
	}
	stub := ctx.GetStub()
	iter, err := stub.GetStateByPartialCompositeKey(voteKeyObjectType, []string{orderId, evidenceHash, verdictVersion})
	if err != nil {
		return nil, fmt.Errorf("查询投票失败: %w", err)
	}
	defer func() { _ = iter.Close() }()

	tally := &VoteTally{
		OrderID:        orderId,
		EvidenceHash:   evidenceHash,
		VerdictVersion: verdictVersion,
		Threshold:      verificationThreshold,
		Votes:          []VoteRecord{},
	}
	for iter.HasNext() {
		kv, err := iter.Next()
		if err != nil {
			return nil, fmt.Errorf("遍历投票失败: %w", err)
		}
		var rec VoteRecord
		if err := json.Unmarshal(kv.Value, &rec); err != nil {
			return nil, fmt.Errorf("解析投票记录 %s 失败: %w", kv.Key, err)
		}
		tally.Votes = append(tally.Votes, rec)
		switch rec.Verdict {
		case "approve":
			tally.ApproveCount++
		case "reject":
			tally.RejectCount++
		}
	}
	switch {
	case tally.ApproveCount >= verificationThreshold:
		tally.Verdict = "approve"
	case tally.RejectCount >= verificationThreshold:
		tally.Verdict = "reject"
	}
	return tally, nil
}

// RecordDeviceEnrollment 登记一张物理 GPU（stage-1 最简去重：gpuUuid 重复即拒绝，
// 奖励在 stage 2）。仅 UserOrg 可调用；owner 由调用者证书派生，参数中不存在 owner。
func (c *DepinContract) RecordDeviceEnrollment(ctx contractapi.TransactionContextInterface, gpuUuid, scoreReportHash string) (*DeviceRecord, error) {
	if gpuUuid == "" || scoreReportHash == "" {
		return nil, fmt.Errorf("gpuUuid/scoreReportHash 均不能为空")
	}
	mspid, clientID, err := callerIdentity(ctx)
	if err != nil {
		return nil, err
	}
	if mspid != userOrgMSP {
		return nil, fmt.Errorf("仅 %s 成员可登记设备，当前调用者 MSP=%s", userOrgMSP, mspid)
	}

	stub := ctx.GetStub()
	key := stateKeyDevicePrefix + gpuUuid
	existing, err := stub.GetState(key)
	if err != nil {
		return nil, fmt.Errorf("读取既有设备记录失败: %w", err)
	}
	if len(existing) > 0 {
		var prev DeviceRecord
		if err := json.Unmarshal(existing, &prev); err != nil {
			return nil, fmt.Errorf("解析既有设备记录失败: %w", err)
		}
		return nil, fmt.Errorf("GPU %s 已登记（owner=%s），同一 gpuUuid 不得重复登记", gpuUuid, prev.Owner)
	}

	rec := &DeviceRecord{
		GpuUUID:         gpuUuid,
		Owner:           clientID,
		OwnerMSPID:      mspid,
		ScoreReportHash: scoreReportHash,
		Status:          "enrolled",
		EnrollTxID:      stub.GetTxID(),
	}
	val, err := json.Marshal(rec)
	if err != nil {
		return nil, err
	}
	if err := stub.PutState(key, val); err != nil {
		return nil, fmt.Errorf("写入设备记录失败: %w", err)
	}
	return rec, nil
}

// GetDevice 按 gpuUuid 查询设备登记记录。
func (c *DepinContract) GetDevice(ctx contractapi.TransactionContextInterface, gpuUuid string) (*DeviceRecord, error) {
	if gpuUuid == "" {
		return nil, fmt.Errorf("gpuUuid 不能为空")
	}
	raw, err := ctx.GetStub().GetState(stateKeyDevicePrefix + gpuUuid)
	if err != nil {
		return nil, fmt.Errorf("读取设备记录失败: %w", err)
	}
	if len(raw) == 0 {
		return nil, fmt.Errorf("设备 %s 不存在", gpuUuid)
	}
	var rec DeviceRecord
	if err := json.Unmarshal(raw, &rec); err != nil {
		return nil, fmt.Errorf("解析设备记录失败: %w", err)
	}
	return &rec, nil
}

func contains(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}
