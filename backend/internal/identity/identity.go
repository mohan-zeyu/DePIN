// Package identity 从 Fabric 身份（证书）派生当前用户身份。
// 约束（spec §10）：绝不读取请求体里的 userId / owner / orgId；
// 当前用户只能来自服务端持有的可信 Fabric 身份（钱包）。
package identity

import (
	"context"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
)

// Identity 是从证书派生的调用者身份。
// ClientID 与链码 WhoAmI / RecordDeviceEnrollment 的口径一致（x509 subject DN）。
type Identity struct {
	Name     string // 钱包内身份名（仅本地引用，不上链）
	MSPID    string // 组织 MSP ID
	CertPEM  []byte // 签名证书（发送给 peer 的就是这张证书）
	clientID string // x509 subject DN，构造时派生，不可外部设置
}

// ClientID 返回证书派生的身份标识（x509 subject DN）。
func (i *Identity) ClientID() string { return i.clientID }

// LoadWalletIdentity 从文件钱包加载身份。
// 钱包布局：<wallet>/<name>/cert.pem、<wallet>/<name>/key.pem、<wallet>/<name>/msp-id。
func LoadWalletIdentity(walletPath, name string) (*Identity, error) {
	if walletPath == "" || name == "" {
		return nil, errors.New("walletPath 与 name 不能为空")
	}
	dir := filepath.Join(walletPath, name)
	certPEM, err := os.ReadFile(filepath.Join(dir, "cert.pem"))
	if err != nil {
		return nil, fmt.Errorf("读取证书失败（%s）: %w", filepath.Join(dir, "cert.pem"), err)
	}
	if _, err := os.ReadFile(filepath.Join(dir, "key.pem")); err != nil {
		return nil, fmt.Errorf("读取私钥失败（%s）: %w", filepath.Join(dir, "key.pem"), err)
	}
	mspIDBytes, err := os.ReadFile(filepath.Join(dir, "msp-id"))
	if err != nil {
		return nil, fmt.Errorf("读取 msp-id 失败（%s）: %w", filepath.Join(dir, "msp-id"), err)
	}
	mspID := strings.TrimSpace(string(mspIDBytes))
	if mspID == "" {
		return nil, fmt.Errorf("msp-id 文件为空（%s）", filepath.Join(dir, "msp-id"))
	}

	block, _ := pem.Decode(certPEM)
	if block == nil || block.Type != "CERTIFICATE" {
		return nil, errors.New("cert.pem 不是合法的证书 PEM")
	}
	cert, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("解析证书失败: %w", err)
	}
	return &Identity{
		Name:     name,
		MSPID:    mspID,
		CertPEM:  certPEM,
		clientID: cert.Subject.String(),
	}, nil
}

// PrivateKeyPEM 返回私钥 PEM（仅 fabric gateway 连接使用，不进入 HTTP 响应）。
func (i *Identity) PrivateKeyPEM(walletPath string) ([]byte, error) {
	return os.ReadFile(filepath.Join(walletPath, i.Name, "key.pem"))
}

type ctxKey struct{}

// Middleware 把服务端加载的可信身份注入请求上下文。
// stage-1 后端以单一配置身份连接 peer，每个请求的“当前用户”即该证书身份；
// 将来扩展为按请求选择身份时，仍只能来自服务端可信映射（如登录会话→钱包），
// 绝不能来自请求体。
func Middleware(id *Identity) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			ctx := context.WithValue(r.Context(), ctxKey{}, id)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// FromContext 取出请求绑定的可信身份。
func FromContext(ctx context.Context) (*Identity, bool) {
	id, ok := ctx.Value(ctxKey{}).(*Identity)
	return id, ok
}
