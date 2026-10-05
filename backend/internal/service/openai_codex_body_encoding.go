package service

import (
	"bytes"
	"io"
	"net/http"
	"strings"
	"sync"

	"github.com/klauspost/compress/zstd"
)

// codexRequestBodyContentEncoding 是官方客户端发往 ChatGPT Codex 后端的请求体编码。
// 官方 HTTP 路径实测：content-encoding: zstd；压缩前后大小随请求正文变化。
// sub2api 此前一律发明文 JSON，属于官方形态中不存在的写法。
const codexRequestBodyContentEncoding = "zstd"

var (
	codexRequestBodyZstdOnce    sync.Once
	codexRequestBodyZstdEncoder *zstd.Encoder
)

// codexRequestBodyZstdWriter 复用同一个 encoder：EncodeAll 支持并发调用，
// 热路径无需加锁，也不必每请求重建窗口缓冲。
func codexRequestBodyZstdWriter() *zstd.Encoder {
	codexRequestBodyZstdOnce.Do(func() {
		// SpeedDefault 与官方所用 zstd 默认等级的压缩率同档（实测正文约 3 倍压缩）。
		encoder, err := zstd.NewWriter(nil, zstd.WithEncoderLevel(zstd.SpeedDefault))
		if err != nil {
			return
		}
		codexRequestBodyZstdEncoder = encoder
	})
	return codexRequestBodyZstdEncoder
}

// applyCodexRequestBodyZstd 把已构造好的出站正文压成官方形态（含编码头与长度）。
// 只对 Codex 协议且发往 ChatGPT Codex 后端的请求生效：其他上游（api.openai.com、
// 国产兼容端点、自定义 base_url）不认这个编码，误加会被直接拒绝。
// 返回 false 表示本次保持明文（无正文、已有编码声明或压缩器不可用）。
func applyCodexRequestBodyZstd(account *Account, req *http.Request) bool {
	if account == nil || req == nil || !account.UsesOpenAICodexProtocol() {
		return false
	}
	if req.Method != http.MethodPost || req.Body == nil || req.GetBody == nil {
		return false
	}
	if req.URL == nil || !strings.EqualFold(req.URL.Hostname(), "chatgpt.com") {
		return false
	}
	if strings.TrimSpace(req.Header.Get("content-encoding")) != "" {
		return false
	}
	encoder := codexRequestBodyZstdWriter()
	if encoder == nil {
		return false
	}
	raw, err := req.GetBody()
	if err != nil {
		return false
	}
	defer func() { _ = raw.Close() }()
	payload, err := io.ReadAll(raw)
	if err != nil || len(payload) == 0 {
		return false
	}
	compressed := encoder.EncodeAll(payload, make([]byte, 0, len(payload)/2))
	req.Header.Set("content-encoding", codexRequestBodyContentEncoding)
	req.Body = io.NopCloser(bytes.NewReader(compressed))
	req.ContentLength = int64(len(compressed))
	// 保留可重放正文：上游重试与插件路径都依赖 GetBody 与 header 声明的编码一致。
	req.GetBody = func() (io.ReadCloser, error) {
		return io.NopCloser(bytes.NewReader(compressed)), nil
	}
	return true
}
