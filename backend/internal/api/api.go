// Package api 提供 stage-1 HTTP 接口：
//
//	GET  /healthz          存活检查
//	GET  /api/v1/whoami    调链码 depin:WhoAmI，核对证书派生身份
//	POST /api/v1/devices   设备登记（调 depin:RecordDeviceEnrollment）
//
// 身份约束（spec §10）：当前用户一律取自请求上下文中由证书派生的 Identity；
// 请求体中的 owner / userId / orgId 等字段被忽略，绝不参与链码参数。
package api

import (
	"encoding/json"
	"io"
	"net/http"
	"strings"

	"github.com/mohan-zeyu/depin/backend/internal/fabric"
	"github.com/mohan-zeyu/depin/backend/internal/identity"
)

const maxBodyBytes = 1 << 20 // 1 MiB

// NewRouter 构造路由并套上身份中间件。
func NewRouter(cc fabric.Client, id *identity.Identity) http.Handler {
	s := &server{cc: cc}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealthz)
	mux.HandleFunc("GET /api/v1/whoami", s.handleWhoAmI)
	mux.HandleFunc("POST /api/v1/devices", s.handleCreateDevice)
	return identity.Middleware(id)(mux)
}

type server struct {
	cc fabric.Client
}

func (s *server) handleHealthz(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok", "service": "depin-backend"})
}

// handleWhoAmI 调链码 WhoAmI 并与服务端证书派生身份核对。
func (s *server) handleWhoAmI(w http.ResponseWriter, r *http.Request) {
	id, ok := identity.FromContext(r.Context())
	if !ok {
		writeError(w, http.StatusInternalServerError, "服务端身份未初始化")
		return
	}
	raw, err := s.cc.Query(r.Context(), "WhoAmI")
	if err != nil {
		writeError(w, http.StatusBadGateway, "链码 WhoAmI 查询失败: "+err.Error())
		return
	}
	var onChain struct {
		MSPID    string `json:"mspid"`
		ClientID string `json:"clientId"`
	}
	if err := json.Unmarshal(raw, &onChain); err != nil {
		writeError(w, http.StatusBadGateway, "链码 WhoAmI 返回非 JSON")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"mspid":          onChain.MSPID,
		"clientId":       onChain.ClientID,
		"walletIdentity": id.Name,
		"certConsistent": onChain.MSPID == id.MSPID && onChain.ClientID == id.ClientID(),
		"identitySource": "fabric-cert", // 身份来源：证书，而非请求体
	})
}

type createDeviceRequest struct {
	GpuUUID         string `json:"gpuUuid"`
	ScoreReportHash string `json:"scoreReportHash"`
	// Owner 字段被【故意】忽略：owner 只能由链码从调用者证书派生（spec §10）。
	// 保留在结构体中仅为了让“忽略”行为显式、可测试。
	Owner any `json:"owner,omitempty"`
	// 其他任何试图声明身份的字段（userId/orgId 等）同样被丢弃（未知字段不入结构体）。
}

func (s *server) handleCreateDevice(w http.ResponseWriter, r *http.Request) {
	id, ok := identity.FromContext(r.Context())
	if !ok {
		writeError(w, http.StatusInternalServerError, "服务端身份未初始化")
		return
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, maxBodyBytes))
	if err != nil {
		writeError(w, http.StatusBadRequest, "读取请求体失败")
		return
	}
	var req createDeviceRequest
	if err := json.Unmarshal(body, &req); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是合法 JSON")
		return
	}
	if strings.TrimSpace(req.GpuUUID) == "" || strings.TrimSpace(req.ScoreReportHash) == "" {
		writeError(w, http.StatusBadRequest, "gpuUuid 与 scoreReportHash 均为必填")
		return
	}

	// 只传 (gpuUuid, scoreReportHash)；owner 由链码从证书派生，永不出现在参数里。
	raw, err := s.cc.Submit(r.Context(), "RecordDeviceEnrollment", req.GpuUUID, req.ScoreReportHash)
	if err != nil {
		writeError(w, mapChaincodeError(err), "设备登记失败: "+err.Error())
		return
	}

	// 透传链码返回的 DeviceRecord（其 owner 字段即证书派生的调用者身份）。
	var device map[string]any
	if err := json.Unmarshal(raw, &device); err != nil {
		writeError(w, http.StatusBadGateway, "链码返回非 JSON")
		return
	}
	// 响应头显式标注本次请求实际使用的证书身份，便于联调核对“owner 来自证书”。
	w.Header().Set("X-Depin-Cert-Owner", id.ClientID())
	w.Header().Set("X-Depin-Cert-MSP", id.MSPID)
	writeJSON(w, http.StatusCreated, device)
}

// mapChaincodeError 把链码错误映射为 HTTP 状态码（按稳定错误文案片段）。
func mapChaincodeError(err error) int {
	msg := err.Error()
	switch {
	case strings.Contains(msg, "不能为空"):
		return http.StatusBadRequest
	case strings.Contains(msg, "仅 UserOrg"):
		return http.StatusForbidden
	case strings.Contains(msg, "不得重复登记"), strings.Contains(msg, "已登记"):
		return http.StatusConflict
	default:
		return http.StatusBadGateway
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}
