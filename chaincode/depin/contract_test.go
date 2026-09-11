// contract_test.go 使用 mock transaction context 覆盖 stage-1 的身份隔离行为，
// 对应 issue #13 验收标准的链码层证据：
//  1. 普通 UserOrg 调用 SubmitVerificationVote 被拒绝；
//  2. 同一 MSP 两张不同证书对同一投票键投票，第二张被拒，仍只有 1 票；
//  3. 不同 evidenceHash 的投票分开计数，不能合并成多数；
//  4. RecordDeviceEnrollment 的 owner 来自调用者证书；同 gpuUuid 重复登记被拒；
//  5. 完全相同的重复投票幂等成功且票数不变。
package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"math/big"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/hyperledger/fabric-chaincode-go/v2/shim"
	"github.com/hyperledger/fabric-contract-api-go/v2/contractapi"
	"github.com/hyperledger/fabric-protos-go-apiv2/ledger/queryresult"
	"github.com/hyperledger/fabric-protos-go-apiv2/msp"
	"google.golang.org/protobuf/proto"
)

// ---- mock stub / context ----

// testStub 仅实现合约用到的方法；未实现的方法继承自嵌入接口（调用即 panic，
// 测试会立刻暴露），避免实现完整 ChaincodeStubInterface。
type testStub struct {
	shim.ChaincodeStubInterface
	state   map[string][]byte
	creator []byte
	txID    string
}

func (s *testStub) GetState(key string) ([]byte, error) {
	return s.state[key], nil
}

func (s *testStub) PutState(key string, value []byte) error {
	s.state[key] = value
	return nil
}

func (s *testStub) DelState(key string) error {
	delete(s.state, key)
	return nil
}

func (s *testStub) GetCreator() ([]byte, error) {
	return s.creator, nil
}

func (s *testStub) GetTxID() string {
	return s.txID
}

func (s *testStub) CreateCompositeKey(objectType string, attributes []string) (string, error) {
	return encodeCompositeKey(objectType, attributes), nil
}

func (s *testStub) GetStateByPartialCompositeKey(objectType string, attributes []string) (shim.StateQueryIteratorInterface, error) {
	prefix := encodeCompositeKey(objectType, attributes)
	var keys []string
	for k := range s.state {
		if strings.HasPrefix(k, prefix) {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	iter := &testIter{stub: s, keys: keys}
	return iter, nil
}

// encodeCompositeKey 与 Fabric 复合键格式保持同构（\x00 分隔），只需 mock 内部一致。
func encodeCompositeKey(objectType string, attributes []string) string {
	return "\x00" + objectType + "\x00" + strings.Join(attributes, "\x00")
}

type testIter struct {
	stub *testStub
	keys []string
	idx  int
}

func (it *testIter) HasNext() bool { return it.idx < len(it.keys) }

func (it *testIter) Next() (*queryresult.KV, error) {
	k := it.keys[it.idx]
	it.idx++
	return &queryresult.KV{Namespace: "depincc", Key: k, Value: it.stub.state[k]}, nil
}

func (it *testIter) Close() error { return nil }

// testCtx 覆盖 GetStub，其余继承 TransactionContextInterface。
type testCtx struct {
	contractapi.TransactionContextInterface
	stub *testStub
}

func (c *testCtx) GetStub() shim.ChaincodeStubInterface { return c.stub }

// ledger 让同一测试内的多个身份共享同一份账本状态。
type ledger struct {
	state map[string][]byte
	n     int
}

func newLedger(t *testing.T) *ledger {
	t.Helper()
	return &ledger{state: map[string][]byte{}}
}

// ctxWith 返回以 (mspid, commonName) 自签证书作为调用者身份的新 context。
// 同 mspid 不同 commonName 即“同组织不同证书”。
func (l *ledger) ctxWith(t *testing.T, mspid, commonName string) *testCtx {
	t.Helper()
	l.n++
	creator, err := marshalCreator(mspid, commonName)
	if err != nil {
		t.Fatalf("构造身份失败: %v", err)
	}
	return &testCtx{stub: &testStub{
		state:   l.state,
		creator: creator,
		txID:    fmt.Sprintf("txid-%d-%s", l.n, commonName),
	}}
}

func marshalCreator(mspid, commonName string) ([]byte, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()),
		Subject:      pkix.Name{CommonName: commonName, Organization: []string{mspid}},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		return nil, err
	}
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	return proto.Marshal(&msp.SerializedIdentity{Mspid: mspid, IdBytes: pemBytes})
}

// subjectDN 返回与链码 callerIdentity 相同口径的 x509 subject DN，供断言使用。
func subjectDN(t *testing.T, mspid, commonName string) string {
	t.Helper()
	// 解析 marshalCreator 生成的 PEM，取 Subject.String()。
	creator, err := marshalCreator(mspid, commonName)
	if err != nil {
		t.Fatalf("构造身份失败: %v", err)
	}
	var sid msp.SerializedIdentity
	if err := proto.Unmarshal(creator, &sid); err != nil {
		t.Fatalf("反序列化 SerializedIdentity 失败: %v", err)
	}
	block, _ := pem.Decode(sid.IdBytes)
	if block == nil {
		t.Fatal("IdBytes 不是合法 PEM")
	}
	cert, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		t.Fatalf("解析证书失败: %v", err)
	}
	return cert.Subject.String()
}

// ---- 用例 ----

func TestInitIdempotent(t *testing.T) {
	l := newLedger(t)
	c := &DepinContract{}
	if err := c.Init(l.ctxWith(t, "UserOrg", "admin@user")); err != nil {
		t.Fatalf("首次 Init 失败: %v", err)
	}
	if err := c.Init(l.ctxWith(t, "UserOrg", "user1@user")); err != nil {
		t.Fatalf("重复 Init 应幂等成功: %v", err)
	}
	var meta map[string]int
	if err := json.Unmarshal(l.state[stateKeyNetworkMeta], &meta); err != nil {
		t.Fatalf("解析元数据失败: %v", err)
	}
	if meta["policyVersion"] != policyVersion {
		t.Fatalf("policyVersion = %d, 期望 %d", meta["policyVersion"], policyVersion)
	}
}

func TestWhoAmI(t *testing.T) {
	l := newLedger(t)
	ctx := l.ctxWith(t, "Verify1Org", "admin@verify1")
	res, err := (&DepinContract{}).WhoAmI(ctx)
	if err != nil {
		t.Fatalf("WhoAmI 失败: %v", err)
	}
	if res.MSPID != "Verify1Org" {
		t.Fatalf("mspid = %q, 期望 Verify1Org", res.MSPID)
	}
	if want := subjectDN(t, "Verify1Org", "admin@verify1"); res.ClientID != want {
		t.Fatalf("clientId = %q, 期望证书 subject DN %q", res.ClientID, want)
	}
}

func TestGetPolicy(t *testing.T) {
	l := newLedger(t)
	p, err := (&DepinContract{}).GetPolicy(l.ctxWith(t, "UserOrg", "user1@user"))
	if err != nil {
		t.Fatalf("GetPolicy 失败: %v", err)
	}
	if p.PolicyVersion != 1 || p.Verification.Threshold != 2 || p.Arbitration.Threshold != 2 {
		t.Fatalf("策略常量不符合预期: %+v", p)
	}
	if len(p.Verification.Orgs) != 3 || len(p.Arbitration.Orgs) != 3 {
		t.Fatalf("组织数量不符合 3+3: %+v", p)
	}
	for _, v := range p.Verification.Orgs {
		for _, a := range p.Arbitration.Orgs {
			if v == a {
				t.Fatalf("核验与仲裁组织集合存在重叠: %s", v)
			}
		}
	}
}

// 验收 1：普通 UserOrg 调用 SubmitVerificationVote 被拒绝。
func TestUserOrgCannotVote(t *testing.T) {
	l := newLedger(t)
	ctx := l.ctxWith(t, "UserOrg", "user1@user")
	_, err := (&DepinContract{}).SubmitVerificationVote(ctx, "order-1", "evhash-a", "v1", "approve")
	if err == nil {
		t.Fatal("UserOrg 投票应被拒绝，实际成功")
	}
	if !strings.Contains(err.Error(), "仅核验组织") {
		t.Fatalf("错误信息应说明门禁原因，实际: %v", err)
	}
}

func TestArbiterOrgCannotVoteInVerification(t *testing.T) {
	l := newLedger(t)
	ctx := l.ctxWith(t, "Arbiter1Org", "admin@arbiter1")
	if _, err := (&DepinContract{}).SubmitVerificationVote(ctx, "order-1", "evhash-a", "v1", "approve"); err == nil {
		t.Fatal("仲裁组织参与正常核验投票应被拒绝")
	}
}

func TestVoteVerdictValidation(t *testing.T) {
	l := newLedger(t)
	ctx := l.ctxWith(t, "Verify1Org", "admin@verify1")
	if _, err := (&DepinContract{}).SubmitVerificationVote(ctx, "order-1", "evhash-a", "v1", "maybe"); err == nil {
		t.Fatal("verdict 非 approve/reject 应被拒绝")
	}
	if _, err := (&DepinContract{}).SubmitVerificationVote(ctx, "", "evhash-a", "v1", "approve"); err == nil {
		t.Fatal("orderId 为空应被拒绝")
	}
}

// 验收 2：同一 MSP 两张不同证书投同一键，第二张被拒，仍只有 1 票。
func TestOneVotePerMSPEvenWithDifferentCerts(t *testing.T) {
	l := newLedger(t)
	c := &DepinContract{}

	first, err := c.SubmitVerificationVote(l.ctxWith(t, "Verify1Org", "admin@verify1"), "order-1", "evhash-a", "v1", "approve")
	if err != nil {
		t.Fatalf("第一张证书投票失败: %v", err)
	}
	if first.MSPID != "Verify1Org" || first.Verdict != "approve" {
		t.Fatalf("投票记录不符合预期: %+v", first)
	}

	// 同组织另一张证书（clientId 不同）再投同一 (order, evidence, version)。
	_, err = c.SubmitVerificationVote(l.ctxWith(t, "Verify1Org", "user7@verify1"), "order-1", "evhash-a", "v1", "approve")
	if err == nil {
		t.Fatal("同 MSP 第二张证书投票应被拒绝")
	}
	if !strings.Contains(err.Error(), "每个 MSP 只计一票") {
		t.Fatalf("错误信息应明确“每个 MSP 只计一票”，实际: %v", err)
	}

	tally, err := c.GetVerificationVotes(l.ctxWith(t, "Verify1Org", "admin@verify1"), "order-1", "evhash-a", "v1")
	if err != nil {
		t.Fatalf("计票查询失败: %v", err)
	}
	if len(tally.Votes) != 1 || tally.ApproveCount != 1 {
		t.Fatalf("同组织两张证书后仍应只有 1 票，实际 votes=%d approve=%d", len(tally.Votes), tally.ApproveCount)
	}
	if tally.Verdict != "" {
		t.Fatalf("1 票不应形成多数结论，实际 %q", tally.Verdict)
	}
}

// 验收 3：不同 evidenceHash 的投票分开计数，不能合并成多数。
func TestDifferentEvidenceHashVotesCountedSeparately(t *testing.T) {
	l := newLedger(t)
	c := &DepinContract{}

	// Verify1 投证据 A，Verify2 投证据 B：两张“approve”分属不同证据，不能凑成 2/3。
	mustVote(t, c, l, "Verify1Org", "admin@verify1", "order-1", "evhash-a", "v1", "approve")
	mustVote(t, c, l, "Verify2Org", "admin@verify2", "order-1", "evhash-b", "v1", "approve")

	for _, ev := range []string{"evhash-a", "evhash-b"} {
		tally, err := c.GetVerificationVotes(l.ctxWith(t, "Verify1Org", "admin@verify1"), "order-1", ev, "v1")
		if err != nil {
			t.Fatalf("计票查询失败: %v", err)
		}
		if tally.ApproveCount != 1 || tally.Verdict != "" {
			t.Fatalf("evidence=%s 应只计 1 票且无多数，实际 approve=%d verdict=%q", ev, tally.ApproveCount, tally.Verdict)
		}
	}

	// 同一证据再获一票后才达到门槛。
	mustVote(t, c, l, "Verify3Org", "admin@verify3", "order-1", "evhash-a", "v1", "approve")
	tally, err := c.GetVerificationVotes(l.ctxWith(t, "Verify1Org", "admin@verify1"), "order-1", "evhash-a", "v1")
	if err != nil {
		t.Fatalf("计票查询失败: %v", err)
	}
	if tally.ApproveCount != 2 || tally.Verdict != "approve" {
		t.Fatalf("同一证据 2 票应通过，实际 approve=%d verdict=%q", tally.ApproveCount, tally.Verdict)
	}
}

// 验收 5：完全相同的重复投票幂等成功且票数不变。
func TestDuplicateIdenticalVoteIsIdempotent(t *testing.T) {
	l := newLedger(t)
	c := &DepinContract{}

	first, err := c.SubmitVerificationVote(l.ctxWith(t, "Verify1Org", "admin@verify1"), "order-2", "evhash-a", "v1", "reject")
	if err != nil {
		t.Fatalf("首次投票失败: %v", err)
	}
	again, err := c.SubmitVerificationVote(l.ctxWith(t, "Verify1Org", "admin@verify1"), "order-2", "evhash-a", "v1", "reject")
	if err != nil {
		t.Fatalf("完全相同的重复投票应幂等成功: %v", err)
	}
	if again.TxID != first.TxID {
		t.Fatalf("幂等响应应返回原始投票记录")
	}

	// 同证书同键换结论：拒绝，不允许改票。
	if _, err := c.SubmitVerificationVote(l.ctxWith(t, "Verify1Org", "admin@verify1"), "order-2", "evhash-a", "v1", "approve"); err == nil {
		t.Fatal("同证书改票应被拒绝")
	}

	tally, err := c.GetVerificationVotes(l.ctxWith(t, "Verify1Org", "admin@verify1"), "order-2", "evhash-a", "v1")
	if err != nil {
		t.Fatalf("计票查询失败: %v", err)
	}
	if len(tally.Votes) != 1 || tally.RejectCount != 1 || tally.ApproveCount != 0 {
		t.Fatalf("重复投票后票数应不变，实际 votes=%d reject=%d approve=%d", len(tally.Votes), tally.RejectCount, tally.ApproveCount)
	}
}

// 验收 4：owner 来自调用者证书而非参数；同 gpuUuid 重复登记被拒。
func TestRecordDeviceEnrollmentOwnerFromCert(t *testing.T) {
	l := newLedger(t)
	c := &DepinContract{}

	ctxUser1 := l.ctxWith(t, "UserOrg", "user1@user")
	rec, err := c.RecordDeviceEnrollment(ctxUser1, "GPU-UUID-0001", "scorehash-1")
	if err != nil {
		t.Fatalf("设备登记失败: %v", err)
	}
	if want := subjectDN(t, "UserOrg", "user1@user"); rec.Owner != want {
		t.Fatalf("owner 应为调用者证书 DN %q，实际 %q", want, rec.Owner)
	}
	if rec.OwnerMSPID != "UserOrg" {
		t.Fatalf("ownerMspid = %q, 期望 UserOrg", rec.OwnerMSPID)
	}

	// 换另一个用户证书登记另一张卡：owner 随证书变化（且 API 不存在 owner 参数可伪造）。
	rec2, err := c.RecordDeviceEnrollment(l.ctxWith(t, "UserOrg", "user2@user"), "GPU-UUID-0002", "scorehash-2")
	if err != nil {
		t.Fatalf("第二张卡登记失败: %v", err)
	}
	if rec2.Owner == rec.Owner {
		t.Fatal("不同调用者证书应派生不同 owner")
	}

	// 同 gpuUuid 重复登记（无论谁提交）被拒。
	if _, err := c.RecordDeviceEnrollment(l.ctxWith(t, "UserOrg", "user2@user"), "GPU-UUID-0001", "scorehash-1"); err == nil {
		t.Fatal("同 gpuUuid 重复登记应被拒绝")
	}

	// 非用户组织不能登记设备。
	if _, err := c.RecordDeviceEnrollment(l.ctxWith(t, "Verify1Org", "admin@verify1"), "GPU-UUID-0003", "scorehash-3"); err == nil {
		t.Fatal("核验组织登记设备应被拒绝")
	}

	// GetDevice 能读回登记记录。
	got, err := c.GetDevice(l.ctxWith(t, "Arbiter2Org", "admin@arbiter2"), "GPU-UUID-0001")
	if err != nil {
		t.Fatalf("GetDevice 失败: %v", err)
	}
	if got.GpuUUID != "GPU-UUID-0001" || got.ScoreReportHash != "scorehash-1" || got.Status != "enrolled" {
		t.Fatalf("设备记录不符合预期: %+v", got)
	}
	if _, err := c.GetDevice(l.ctxWith(t, "UserOrg", "user1@user"), "GPU-UUID-404"); err == nil {
		t.Fatal("查询不存在的设备应报错")
	}
}

// 门槛与不同 verdictVersion 分开计数的补充验证。
func TestThresholdAndVersionSeparation(t *testing.T) {
	l := newLedger(t)
	c := &DepinContract{}

	// 2 个 reject 达到门槛。
	mustVote(t, c, l, "Verify1Org", "admin@verify1", "order-3", "evhash-a", "v1", "reject")
	mustVote(t, c, l, "Verify2Org", "admin@verify2", "order-3", "evhash-a", "v1", "reject")
	tally, err := c.GetVerificationVotes(l.ctxWith(t, "UserOrg", "user1@user"), "order-3", "evhash-a", "v1")
	if err != nil {
		t.Fatalf("计票查询失败: %v", err)
	}
	if tally.Verdict != "reject" {
		t.Fatalf("2 票 reject 应形成 reject 结论，实际 %q", tally.Verdict)
	}

	// 同订单同证据的 v2 版本重新计票，不继承 v1。
	tallyV2, err := c.GetVerificationVotes(l.ctxWith(t, "UserOrg", "user1@user"), "order-3", "evhash-a", "v2")
	if err != nil {
		t.Fatalf("计票查询失败: %v", err)
	}
	if len(tallyV2.Votes) != 0 || tallyV2.Verdict != "" {
		t.Fatalf("不同 verdictVersion 应从零计票，实际 votes=%d verdict=%q", len(tallyV2.Votes), tallyV2.Verdict)
	}
}

func mustVote(t *testing.T, c *DepinContract, l *ledger, mspid, cn, order, evidence, version, verdict string) {
	t.Helper()
	if _, err := c.SubmitVerificationVote(l.ctxWith(t, mspid, cn), order, evidence, version, verdict); err != nil {
		t.Fatalf("%s/%s 投票失败: %v", mspid, cn, err)
	}
}
