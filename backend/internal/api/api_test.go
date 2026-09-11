// api_test.go — HTTP 层单元测试（mock 链码客户端，不依赖真实网络）。
// 重点：POST /api/v1/devices 请求体里伪造的 owner 字段必须被忽略，
// 链码参数只允许 (gpuUuid, scoreReportHash)，owner 只来自服务端证书身份。
package api

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/mohan-zeyu/depin/backend/internal/identity"
)

// mockClient 记录调用并按预设函数返回，模拟链码行为。
type mockClient struct {
	mu       sync.Mutex
	queryFn  func(ctx context.Context, function string, args ...string) ([]byte, error)
	submitFn func(ctx context.Context, function string, args ...string) ([]byte, error)

	submitted []recordedCall
}

type recordedCall struct {
	Function string
	Args     []string
}

func (m *mockClient) Query(ctx context.Context, function string, args ...string) ([]byte, error) {
	if m.queryFn == nil {
		return nil, errors.New("mockClient: 未配置 queryFn")
	}
	return m.queryFn(ctx, function, args...)
}

func (m *mockClient) Submit(ctx context.Context, function string, args ...string) ([]byte, error) {
	m.mu.Lock()
	m.submitted = append(m.submitted, recordedCall{Function: function, Args: args})
	m.mu.Unlock()
	if m.submitFn == nil {
		return nil, errors.New("mockClient: 未配置 submitFn")
	}
	return m.submitFn(ctx, function, args...)
}

func (m *mockClient) submissions() []recordedCall {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]recordedCall, len(m.submitted))
	copy(out, m.submitted)
	return out
}

// testIdentity 构造一个固定证书身份（不读任何请求体字段）。
func testIdentity(t *testing.T) *identity.Identity {
	t.Helper()
	wallet := createTestWallet(t, "UserOrg", "user1@user.depin.dev")
	id, err := identity.LoadWalletIdentity(wallet, "user1")
	if err != nil {
		t.Fatalf("构造测试身份失败: %v", err)
	}
	return id
}

// createTestWallet 生成最小合法钱包（自签证书 + 私钥 + msp-id）。
func createTestWallet(t *testing.T, mspID, commonName string) string {
	t.Helper()
	dir := t.TempDir()
	identDir := filepath.Join(dir, "user1")
	if err := os.MkdirAll(identDir, 0o755); err != nil {
		t.Fatal(err)
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: commonName, Organization: []string{mspID}},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	files := map[string][]byte{
		"cert.pem": pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}),
		"msp-id":   []byte(mspID),
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	files["key.pem"] = pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER})
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(identDir, name), content, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func TestHealthz(t *testing.T) {
	router := NewRouter(&mockClient{}, testIdentity(t))
	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("healthz 状态码 = %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"status":"ok"`) {
		t.Fatalf("healthz 响应异常: %s", rec.Body.String())
	}
}

func TestWhoAmiCallsChaincode(t *testing.T) {
	mc := &mockClient{queryFn: func(_ context.Context, fn string, _ ...string) ([]byte, error) {
		if fn != "WhoAmI" {
			return nil, errors.New("期望调用 WhoAmI，实际 " + fn)
		}
		return json.Marshal(map[string]string{"mspid": "UserOrg", "clientId": "CN=user1@user.depin.dev,O=UserOrg"})
	}}
	id := testIdentity(t)
	router := NewRouter(mc, id)

	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/v1/whoami", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("whoami 状态码 = %d, body=%s", rec.Code, rec.Body.String())
	}
	var body struct {
		MSPID          string `json:"mspid"`
		ClientID       string `json:"clientId"`
		CertConsistent bool   `json:"certConsistent"`
		IdentitySource string `json:"identitySource"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("解析响应失败: %v", err)
	}
	if body.MSPID != "UserOrg" || body.ClientID != "CN=user1@user.depin.dev,O=UserOrg" {
		t.Fatalf("whoami 响应身份不符: %+v", body)
	}
	if !body.CertConsistent {
		t.Fatalf("本地证书派生身份与链上身份应一致: %+v", body)
	}
	if body.IdentitySource != "fabric-cert" {
		t.Fatalf("身份来源应为 fabric-cert: %+v", body)
	}
}

// 核心用例：请求体伪造 owner/orgId/userId，链码参数必须只有 (gpuUuid, scoreReportHash)。
func TestCreateDeviceIgnoresForgedOwnerField(t *testing.T) {
	const certDN = "CN=user1@user.depin.dev,O=UserOrg"
	mc := &mockClient{submitFn: func(_ context.Context, fn string, args ...string) ([]byte, error) {
		if fn != "RecordDeviceEnrollment" {
			return nil, errors.New("期望调用 RecordDeviceEnrollment，实际 " + fn)
		}
		if len(args) != 2 {
			return nil, errors.New("RecordDeviceEnrollment 只应有两个参数")
		}
		// 模拟真实链码：owner 由证书派生，与请求体无关。
		return json.Marshal(map[string]any{
			"gpuUuid":         args[0],
			"owner":           certDN, // 证书派生，不是请求体里的 "attacker"
			"ownerMspid":      "UserOrg",
			"scoreReportHash": args[1],
			"status":          "enrolled",
		})
	}}
	id := testIdentity(t)
	router := NewRouter(mc, id)

	body := `{
		"gpuUuid": "GPU-UUID-0001",
		"scoreReportHash": "scorehash-1",
		"owner": "attacker",
		"orgId": "Verify1Org",
		"userId": "someone-else"
	}`
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/devices", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(rec, req)

	if rec.Code != http.StatusCreated {
		t.Fatalf("设备登记状态码 = %d, body=%s", rec.Code, rec.Body.String())
	}

	// 断言 1：链码参数绝不含 owner/orgId/userId。
	subs := mc.submissions()
	if len(subs) != 1 {
		t.Fatalf("应恰好提交一次，实际 %d", len(subs))
	}
	if subs[0].Function != "RecordDeviceEnrollment" {
		t.Fatalf("调用的函数 = %q", subs[0].Function)
	}
	if got := subs[0].Args; len(got) != 2 || got[0] != "GPU-UUID-0001" || got[1] != "scorehash-1" {
		t.Fatalf("链码参数应只有 (gpuUuid, scoreReportHash)，实际 %v", got)
	}

	// 断言 2：响应中的 owner 是证书身份，不是伪造值。
	var device struct {
		Owner string `json:"owner"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &device); err != nil {
		t.Fatal(err)
	}
	if device.Owner != certDN {
		t.Fatalf("owner 应为证书派生 %q，实际 %q", certDN, device.Owner)
	}
	if device.Owner == "attacker" {
		t.Fatal("owner 不得来自请求体")
	}

	// 断言 3：响应头标注实际使用的证书身份。
	if got := rec.Header().Get("X-Depin-Cert-Owner"); got != certDN {
		t.Fatalf("X-Depin-Cert-Owner = %q, 期望 %q", got, certDN)
	}
}

func TestCreateDeviceValidation(t *testing.T) {
	router := NewRouter(&mockClient{}, testIdentity(t))

	for _, body := range []string{
		`{"gpuUuid": "", "scoreReportHash": "h"}`,
		`{"gpuUuid": "GPU-1", "scoreReportHash": ""}`,
		`not-json`,
	} {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, "/api/v1/devices", strings.NewReader(body))
		router.ServeHTTP(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("非法请求体应返回 400，body=%q 实际 %d", body, rec.Code)
		}
	}
}

func TestCreateDeviceErrorMapping(t *testing.T) {
	cases := []struct {
		name     string
		err      error
		wantCode int
	}{
		{"重复登记", errors.New("GPU GPU-1 已登记（owner=x），同一 gpuUuid 不得重复登记"), http.StatusConflict},
		{"非用户组织", errors.New("仅 UserOrg 成员可登记设备，当前调用者 MSP=Verify1Org"), http.StatusForbidden},
		{"链码失败", errors.New("endorment 失败"), http.StatusBadGateway},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			mc := &mockClient{submitFn: func(context.Context, string, ...string) ([]byte, error) {
				return nil, tc.err
			}}
			router := NewRouter(mc, testIdentity(t))
			rec := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodPost, "/api/v1/devices",
				strings.NewReader(`{"gpuUuid":"GPU-1","scoreReportHash":"h"}`))
			router.ServeHTTP(rec, req)
			if rec.Code != tc.wantCode {
				t.Fatalf("状态码 = %d, 期望 %d (body=%s)", rec.Code, tc.wantCode, rec.Body.String())
			}
		})
	}
}

func TestWhoAmiChaincodeFailure(t *testing.T) {
	mc := &mockClient{queryFn: func(context.Context, string, ...string) ([]byte, error) {
		return nil, errors.New("connection refused")
	}}
	router := NewRouter(mc, testIdentity(t))
	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/v1/whoami", nil))
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("链码不可达应返回 502，实际 %d", rec.Code)
	}
}
