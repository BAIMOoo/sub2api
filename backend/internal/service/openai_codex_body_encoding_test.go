package service

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/klauspost/compress/zstd"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// readCodexUpstreamBody 返回测试替身捕获到的出站正文副本：Codex 协议请求体是
// zstd 编码的官方形态，断言前解开才能继续按 JSON 语义比较。
func readCodexUpstreamBody(headers http.Header, body []byte) []byte {
	if len(body) == 0 || !strings.Contains(strings.ToLower(headers.Get("content-encoding")), "zstd") {
		return append([]byte(nil), body...)
	}
	decoder, err := zstd.NewReader(nil)
	if err != nil {
		return append([]byte(nil), body...)
	}
	defer decoder.Close()
	decoded, err := decoder.DecodeAll(body, nil)
	if err != nil {
		return append([]byte(nil), body...)
	}
	return decoded
}

func codexBodyEncodingTestAccount(id int64) *Account {
	return &Account{
		ID:       id,
		Name:     "codex-body-encoding",
		Platform: PlatformOpenAI,
		Type:     AccountTypeOAuth,
		Credentials: map[string]any{
			"access_token":       "oauth-token",
			"chatgpt_account_id": "chatgpt-acc",
		},
	}
}

func TestApplyCodexRequestBodyZstdCompressesChatGPTPost(t *testing.T) {
	payload := []byte(`{"model":"gpt-5.6-sol","input":[{"type":"input_text","text":"hello"}]}`)
	req, err := http.NewRequest(http.MethodPost, chatgptCodexURL, bytes.NewReader(payload))
	require.NoError(t, err)

	require.True(t, applyCodexRequestBodyZstd(codexBodyEncodingTestAccount(1), req))
	require.Equal(t, codexRequestBodyContentEncoding, req.Header.Get("content-encoding"))

	wire, err := io.ReadAll(req.Body)
	require.NoError(t, err)
	require.Less(t, len(wire), len(payload)+64, "压缩后不应比原文更大（含帧头）")
	require.Equal(t, int64(len(wire)), req.ContentLength, "Content-Length 必须与压缩后的正文一致")

	decoder, err := zstd.NewReader(nil)
	require.NoError(t, err)
	defer decoder.Close()
	decoded, err := decoder.DecodeAll(wire, nil)
	require.NoError(t, err)
	require.JSONEq(t, string(payload), string(decoded))

	// GetBody 必须给出与正文同一份编码后的字节，供重试与插件路径重放。
	replay, err := req.GetBody()
	require.NoError(t, err)
	defer replay.Close()
	replayed, err := io.ReadAll(replay)
	require.NoError(t, err)
	require.Equal(t, wire, replayed)
}

func TestApplyCodexRequestBodyZstdSkipsNonCodexTargets(t *testing.T) {
	payload := []byte(`{"model":"gpt-5"}`)
	newReq := func(t *testing.T, url string, account *Account, contentType string) *http.Request {
		t.Helper()
		req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(payload))
		require.NoError(t, err)
		if contentType != "" {
			req.Header.Set("content-encoding", contentType)
		}
		return req
	}

	cases := []struct {
		name        string
		req         *http.Request
		account     *Account
		encodingSet string
	}{
		{name: "api_key_account", req: newReq(t, chatgptCodexURL, nil, ""), account: &Account{ID: 2, Platform: PlatformOpenAI, Type: AccountTypeAPIKey}},
		{name: "custom_base_url", req: newReq(t, "https://api.openai.com/v1/responses", nil, ""), account: codexBodyEncodingTestAccount(3)},
		{name: "already_encoded", req: newReq(t, chatgptCodexURL, nil, "gzip"), account: codexBodyEncodingTestAccount(4), encodingSet: "gzip"},
		{name: "nil_account", req: newReq(t, chatgptCodexURL, nil, ""), account: nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			require.False(t, applyCodexRequestBodyZstd(tc.account, tc.req))
			require.Equal(t, tc.encodingSet, tc.req.Header.Get("content-encoding"))
			require.Equal(t, int64(len(payload)), tc.req.ContentLength, "未压缩时正文长度不得被改写")
		})
	}
}

// 出站构建器必须真的用上压缩：/v1/responses 与透传两条主路径各测一次。
func TestCodexUpstreamBuildersCompressBodyForChatGPT(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{"model":"gpt-5.6-sol","stream":true,"prompt_cache_key":"client-session","input":[{"type":"input_text","text":"hello"}]}`)

	newContext := func() *gin.Context {
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
		c.Request.Header.Set("Content-Type", "application/json")
		c.Request.Header.Set("session-id", "client-session")
		return c
	}

	for _, tc := range []struct {
		name  string
		build func(*OpenAIGatewayService, *gin.Context, *Account) (*http.Request, error)
	}{
		{
			name: "responses",
			build: func(svc *OpenAIGatewayService, c *gin.Context, account *Account) (*http.Request, error) {
				return svc.buildUpstreamRequest(c.Request.Context(), c, account, body, "token", true, "client-session", true)
			},
		},
		{
			name: "passthrough",
			build: func(svc *OpenAIGatewayService, c *gin.Context, account *Account) (*http.Request, error) {
				return svc.buildUpstreamRequestOpenAIPassthrough(c.Request.Context(), c, account, body, "token")
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			account := codexBodyEncodingTestAccount(9)
			account.Extra = map[string]any{"openai_oauth_passthrough": true}
			svc := &OpenAIGatewayService{cfg: &config.Config{}}

			req, err := tc.build(svc, newContext(), account)
			require.NoError(t, err)
			require.Equal(t, codexRequestBodyContentEncoding, req.Header.Get("content-encoding"))
			require.Equal(t, "application/json", req.Header.Get("content-type"))

			wire, err := io.ReadAll(req.Body)
			require.NoError(t, err)
			decoded := readCodexUpstreamBody(req.Header, wire)
			require.NotEqual(t, wire, decoded, "正文必须是 zstd 编码")
			require.Contains(t, string(decoded), `"model":"gpt-5.6-sol"`)
			require.Equal(t, int64(len(wire)), req.ContentLength)
		})
	}
}

// codexExecCapturedWire is the stable, credential-free subset of the
// 2026-09-28 codex exec HTTP capture. Session IDs, request IDs, cookies, and
// authorization values are intentionally excluded because they are dynamic or
// sensitive. The request body is compared after zstd decoding because the
// compressed frame is not the protocol's semantic payload.
var codexExecCapturedWire = struct {
	method          string
	url             string
	host            string
	accept          string
	contentType     string
	contentEncoding string
	originator      string
	version         string
	userAgent       string
}{
	method:          http.MethodPost,
	url:             chatgptCodexURL,
	host:            "chatgpt.com",
	accept:          "text/event-stream",
	contentType:     "application/json",
	contentEncoding: "zstd",
	originator:      "codex_exec",
	version:         "0.146.0",
	userAgent:       "codex_exec/0.146.0 (Ubuntu 24.4.0; x86_64) xterm-256color (codex_exec; 0.146.0)",
}

func TestCodexExecCapturedWireShapeMatchesSub2APIOutboundRequest(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{"model":"gpt-5.6-sol","stream":true,"input":[{"type":"message","role":"developer","content":"hello"}]}`)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")

	account := codexBodyEncodingTestAccount(42)
	svc := &OpenAIGatewayService{cfg: &config.Config{}}
	req, err := svc.buildUpstreamRequest(c.Request.Context(), c, account, body, "token", true, "", true)
	require.NoError(t, err)

	// Compare the stable wire shape observed from codex exec. Dynamic identity
	// fields are checked separately below, while secrets are never asserted.
	require.Equal(t, codexExecCapturedWire.method, req.Method)
	require.Equal(t, codexExecCapturedWire.url, req.URL.String())
	require.Equal(t, codexExecCapturedWire.host, req.Host)
	require.Equal(t, codexExecCapturedWire.accept, req.Header.Get("accept"))
	require.Equal(t, codexExecCapturedWire.contentType, req.Header.Get("content-type"))
	require.Equal(t, codexExecCapturedWire.contentEncoding, req.Header.Get("content-encoding"))
	require.Equal(t, codexExecCapturedWire.originator, req.Header.Get("originator"))
	require.Equal(t, codexExecCapturedWire.version, req.Header.Get("version"))
	require.Equal(t, codexExecCapturedWire.userAgent, req.Header.Get("user-agent"))

	wire, err := io.ReadAll(req.Body)
	require.NoError(t, err)
	decoded := readCodexUpstreamBody(req.Header, wire)
	require.NotEqual(t, wire, decoded, "Codex exec 的 HTTP 正文应以 zstd 编码")
	require.JSONEq(t, string(body), string(decoded))
	require.False(t, gjson.GetBytes(decoded, "instructions").Exists(), "codex exec 请求没有顶层 instructions")
	require.False(t, gjson.GetBytes(decoded, "tools").Exists(), "codex exec 请求没有顶层 tools")
	require.Equal(t, "developer", gjson.GetBytes(decoded, "input.0.role").String())
}

// TestCodexExecComparisonReport exercises both HTTP builders with the same
// credential-free Responses payload and prints only stable wire metadata. The
// report is intentionally useful when comparing a fresh official codex exec
// capture without ever printing authorization, cookies, or request contents.
func TestCodexExecComparisonReport(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body := []byte(`{"client_metadata":{"os":"linux","arch":"x86_64"},"include":["reasoning.encrypted_content"],"input":[{"type":"message","role":"developer","content":"Reply with exactly OK."}],"model":"gpt-6.1-sol","parallel_tool_calls":true,"prompt_cache_key":"capture-session","reasoning":{"effort":"medium"},"store":false,"stream":true,"text":{"format":{"type":"text"}},"tool_choice":"auto"}`)

	newContext := func() *gin.Context {
		rec := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(rec)
		c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
		c.Request.Header.Set("Content-Type", "application/json")
		c.Request.Header.Set("session-id", "capture-session")
		c.Request.Header.Set("thread-id", "capture-thread")
		c.Request.Header.Set("x-client-request-id", "capture-thread")
		return c
	}

	type report struct {
		Path            string            `json:"path"`
		Method          string            `json:"method"`
		Host            string            `json:"host"`
		Headers         map[string]string `json:"headers"`
		WireBytes       int               `json:"wire_bytes"`
		DecodedBytes    int               `json:"decoded_bytes"`
		DecodedTopLevel []string          `json:"decoded_top_level_keys"`
		Instructions    bool              `json:"has_instructions"`
		Tools           bool              `json:"has_tools"`
		FirstInputRole  string            `json:"first_input_role"`
	}

	build := func(name string, builder func(*OpenAIGatewayService, *gin.Context, *Account) (*http.Request, error)) report {
		t.Helper()
		svc := &OpenAIGatewayService{cfg: &config.Config{}}
		req, err := builder(svc, newContext(), codexBodyEncodingTestAccount(43))
		require.NoError(t, err)
		wire, err := io.ReadAll(req.Body)
		require.NoError(t, err)
		decoded := readCodexUpstreamBody(req.Header, wire)
		var object map[string]any
		require.NoError(t, json.Unmarshal(decoded, &object))
		keys := make([]string, 0, len(object))
		for key := range object {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		headers := map[string]string{}
		for _, key := range []string{"accept", "content-type", "content-encoding", "originator", "version", "user-agent", "x-codex-turn-metadata"} {
			headers[key] = req.Header.Get(key)
		}
		for _, key := range []string{"session-id", "thread-id", "x-client-request-id"} {
			if req.Header.Get(key) != "" {
				headers[key] = "<dynamic>"
			}
		}
		report := report{
			Path:            name,
			Method:          req.Method,
			Host:            req.Host,
			Headers:         headers,
			WireBytes:       len(wire),
			DecodedBytes:    len(decoded),
			DecodedTopLevel: keys,
			Instructions:    gjson.GetBytes(decoded, "instructions").Exists(),
			Tools:           gjson.GetBytes(decoded, "tools").Exists(),
			FirstInputRole:  gjson.GetBytes(decoded, "input.0.role").String(),
		}
		return report
	}

	reports := []report{
		build("responses", func(svc *OpenAIGatewayService, c *gin.Context, account *Account) (*http.Request, error) {
			return svc.buildUpstreamRequest(c.Request.Context(), c, account, body, "token", true, "capture-session", true)
		}),
		build("passthrough", func(svc *OpenAIGatewayService, c *gin.Context, account *Account) (*http.Request, error) {
			return svc.buildUpstreamRequestOpenAIPassthrough(c.Request.Context(), c, account, body, "token")
		}),
	}
	for _, got := range reports {
		require.Equal(t, http.MethodPost, got.Method)
		require.Equal(t, "chatgpt.com", got.Host)
		require.Equal(t, "text/event-stream", got.Headers["accept"])
		require.Equal(t, "application/json", got.Headers["content-type"])
		require.Equal(t, "zstd", got.Headers["content-encoding"])
		require.Equal(t, "codex_exec", got.Headers["originator"])
		require.Equal(t, CodexCanonicalClientVersion(), got.Headers["version"])
		require.NotEmpty(t, got.Headers["user-agent"])
		require.Equal(t, "developer", got.FirstInputRole)
		require.False(t, got.Instructions)
		require.False(t, got.Tools)
		t.Logf("codex HTTP comparison: %s", mustJSON(t, got))
	}
	require.Equal(t, reports[0].Method, reports[1].Method)
	require.Equal(t, reports[0].Host, reports[1].Host)
	require.Equal(t, reports[0].Headers, reports[1].Headers)
	require.Equal(t, reports[0].DecodedTopLevel, reports[1].DecodedTopLevel)
	require.Equal(t, reports[0].Instructions, reports[1].Instructions)
	require.Equal(t, reports[0].Tools, reports[1].Tools)
	require.Equal(t, reports[0].FirstInputRole, reports[1].FirstInputRole)
}

func mustJSON(t *testing.T, value any) string {
	t.Helper()
	encoded, err := json.Marshal(value)
	require.NoError(t, err)
	return string(encoded)
}
