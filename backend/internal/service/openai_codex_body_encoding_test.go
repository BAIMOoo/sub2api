package service

import (
	"bytes"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/klauspost/compress/zstd"
	"github.com/stretchr/testify/require"
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
