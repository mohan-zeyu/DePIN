// identity_test.go — 身份派生逻辑的单元测试：
// MSP ID 来自 msp-id 文件、ClientID 来自证书 subject DN、坏钱包报错。
package identity

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// writeTestWallet 生成一个最小合法钱包（自签证书 + 私钥 + msp-id）。
func writeTestWallet(t *testing.T, mspID, commonName string) string {
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
	if err := os.WriteFile(filepath.Join(identDir, "cert.pem"), pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o644); err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(identDir, "key.pem"), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER}), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(identDir, "msp-id"), []byte(mspID+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

func TestLoadWalletIdentityDerivesMSPAndClientID(t *testing.T) {
	wallet := writeTestWallet(t, "UserOrg", "user1@user.depin.dev")
	id, err := LoadWalletIdentity(wallet, "user1")
	if err != nil {
		t.Fatalf("加载身份失败: %v", err)
	}
	if id.MSPID != "UserOrg" {
		t.Fatalf("MSPID = %q, 期望 UserOrg（必须来自 msp-id 文件）", id.MSPID)
	}
	// ClientID 必须是证书 subject DN，而不是任何请求体或调用方提供的字符串。
	if id.ClientID() != "CN=user1@user.depin.dev,O=UserOrg" {
		t.Fatalf("ClientID = %q, 期望证书 subject DN", id.ClientID())
	}
	if id.Name != "user1" {
		t.Fatalf("Name = %q", id.Name)
	}
}

func TestLoadWalletIdentityRejectsMissingFiles(t *testing.T) {
	wallet := writeTestWallet(t, "UserOrg", "user1@user.depin.dev")
	if err := os.Remove(filepath.Join(wallet, "user1", "cert.pem")); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadWalletIdentity(wallet, "user1"); err == nil {
		t.Fatal("缺少 cert.pem 应报错")
	}

	if _, err := LoadWalletIdentity(wallet, "no-such-identity"); err == nil {
		t.Fatal("不存在的身份应报错")
	}
	if _, err := LoadWalletIdentity("", ""); err == nil {
		t.Fatal("空参数应报错")
	}
}

func TestLoadWalletIdentityRejectsEmptyMspID(t *testing.T) {
	wallet := writeTestWallet(t, "UserOrg", "user1@user.depin.dev")
	if err := os.WriteFile(filepath.Join(wallet, "user1", "msp-id"), []byte("   \n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadWalletIdentity(wallet, "user1"); err == nil {
		t.Fatal("空的 msp-id 应报错")
	}
}

func TestMiddlewareInjectsIdentity(t *testing.T) {
	wallet := writeTestWallet(t, "UserOrg", "user1@user.depin.dev")
	id, err := LoadWalletIdentity(wallet, "user1")
	if err != nil {
		t.Fatal(err)
	}

	var got *Identity
	var seen bool
	handler := Middleware(id)(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		got, seen = FromContext(r.Context())
	}))

	// 中间件之后拿到的身份只能是服务端注入的那份。
	req := httptest.NewRequest(http.MethodGet, "/api/v1/whoami", nil)
	handler.ServeHTTP(httptest.NewRecorder(), req)
	if !seen || got == nil || got.ClientID() != id.ClientID() || got.MSPID != id.MSPID {
		t.Fatalf("中间件注入的身份不符: seen=%v got=%+v", seen, got)
	}

	// 未经中间件的原始请求不携带身份。
	if _, ok := FromContext(req.Context()); ok {
		t.Fatal("未经中间件的请求不应携带身份")
	}
}
