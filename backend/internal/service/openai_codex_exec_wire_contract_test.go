package service

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// Captured official Linux codex exec 0.171.0 requests use synthetic OAuth auth
// against a loopback capture server inside isolated Alpine Linux x86_64. Replay
// their complete JSON (Standard, Lite, and Compact V2) through both OAuth
// gateway paths, preserving fields except account-scoped identities.
// These fixtures prove body preservation, not live supplier or TLS equality.
// Do not parallelize: the test temporarily pins the process-wide identity resolver.
func TestForward_CodexExec0171NativeWireLayouts(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const userAgent = "codex_exec/0.171.0 (Alpine Linux 3.24.4; x86_64) unknown (codex_exec; 0.171.0)"
	codexCanonicalUAMu.RLock()
	previousResolver := codexCanonicalUAResolver
	codexCanonicalUAMu.RUnlock()
	SetCodexCanonicalUserAgentResolver(func() string {
		return "codex_exec/0.163.0 (Ubuntu 24.4.0; x86_64) xterm-256color (codex_exec; 0.163.0)"
	})
	t.Cleanup(func() { SetCodexCanonicalUserAgentResolver(previousResolver) })

	for _, tc := range []struct {
		name    string
		fixture string
		path    string
		lite    bool
		compact bool
	}{
		{name: "standard", fixture: "standard.json", path: "/v1/responses"},
		{name: "lite", fixture: "lite.json", path: "/v1/responses", lite: true},
		{name: "compact", fixture: "compact.json", path: "/v1/responses/compact", compact: true},
	} {
		for _, passthrough := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/passthrough=%v", tc.name, passthrough), func(t *testing.T) {
				body, err := os.ReadFile(filepath.Join("testdata", "codex_exec_0171_linux_amd64", tc.fixture))
				require.NoError(t, err)
				var request map[string]any
				require.NoError(t, json.Unmarshal(body, &request))
				c, _ := gin.CreateTestContext(httptest.NewRecorder())
				c.Request = httptest.NewRequest(http.MethodPost, tc.path, bytes.NewReader(body))
				c.Request.Header.Set("Content-Type", "application/json")
				c.Request.Header.Set("User-Agent", userAgent)
				c.Request.Header.Set("originator", "codex_exec")
				c.Request.Header.Set("version", "0.171.0")
				sessionID, ok := request["prompt_cache_key"].(string)
				require.True(t, ok)
				c.Request.Header.Set("session-id", sessionID)
				metadata, ok := request["client_metadata"].(map[string]any)
				require.True(t, ok)
				for header, field := range map[string]string{
					"thread-id": "thread_id", "x-codex-window-id": "x-codex-window-id", "x-codex-turn-metadata": "x-codex-turn-metadata",
				} {
					value, ok := metadata[field].(string)
					require.True(t, ok, field)
					c.Request.Header.Set(header, value)
				}
				if tc.lite {
					c.Request.Header.Set(responsesLiteHeader, "true")
				}
				SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
				reply := "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_native\",\"output\":[],\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}}\n\ndata: [DONE]\n\n"
				if tc.compact {
					reply = codexHTTPCompactSSEItem + codexHTTPCompactSSECompleted
				}
				upstream := &httpUpstreamRecorder{resp: &http.Response{
					StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{"text/event-stream"}},
					Body: io.NopCloser(strings.NewReader(reply)),
				}}
				account := codexBodyEncodingTestAccount(171)
				account.Status, account.Schedulable, account.Concurrency = StatusActive, true, 1
				account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
				setCodexHTTPTestMode(account, gjson.GetBytes(body, "model").String(), tc.lite)
				svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
				result, err := svc.Forward(context.Background(), c, account, body)
				require.NoError(t, err)
				require.NotNil(t, result)
				require.NotNil(t, upstream.lastReq)
				require.Equal(t, chatgptCodexURL, upstream.lastReq.URL.String())
				require.Equal(t, "codex_exec", upstream.lastReq.Header.Get("originator"))
				require.Equal(t, codexExecHTTPUserAgent(), upstream.lastReq.Header.Get("user-agent"))
				require.Equal(t, codexExecHTTPVersion, upstream.lastReq.Header.Get("version"))
				require.Equal(t, codexExecHTTPAcceptEncoding, upstream.lastReq.Header.Get("accept-encoding"))
				require.Equal(t, codexRequestBodyContentEncoding, upstream.lastReq.Header.Get("content-encoding"))
				require.Equal(t, tc.lite, isOpenAIResponsesLiteHeader(upstream.lastReq.Header.Get(responsesLiteHeader)))
				var forwarded map[string]any
				require.NoError(t, json.Unmarshal(upstream.lastBody, &forwarded))
				require.NotContains(t, forwarded, "instructions")
				cacheKey := gjson.GetBytes(upstream.lastBody, "prompt_cache_key").String()
				require.NotEmpty(t, cacheKey)
				require.Equal(t, cacheKey, upstream.lastReq.Header.Get("session-id"))
				forwardedMetadata, ok := forwarded["client_metadata"].(map[string]any)
				require.True(t, ok)
				require.Equal(t, cacheKey, forwardedMetadata["session_id"])
				require.Equal(t, cacheKey, forwardedMetadata["thread_id"])
				require.Equal(t, forwardedMetadata["turn_id"], forwardedMetadata["root_turn_id"])
				require.Equal(t, forwardedMetadata["x-codex-window-id"], upstream.lastReq.Header.Get("x-codex-window-id"))
				require.Empty(t, upstream.lastReq.Header.Get("x-codex-installation-id"))
				input, ok := request["input"].([]any)
				require.True(t, ok)
				forwardedInput, ok := forwarded["input"].([]any)
				require.True(t, ok)
				require.Len(t, forwardedInput, len(input))
				maskCodexExecScopedIdentities(t, request)
				maskCodexExecScopedIdentities(t, forwarded)
				require.Equal(t, request, forwarded, "native 0.171.0 request fields, prompts, tools, and item IDs must survive forwarding")
			})
		}
	}
}

// Legacy inbound 0.158.0 Standard requests carried top-level instructions.
// Verify they are losslessly promoted to a leading developer message under the
// 0.171.0 Standard wire layout while keeping all original input items and tools.
func TestForward_CodexExec0158LegacyStandardInstructionsPromotedTo0171Layout(t *testing.T) {
	gin.SetMode(gin.TestMode)
	body, err := os.ReadFile(filepath.Join("testdata", "codex_exec_0158", "standard.json"))
	require.NoError(t, err)
	var legacy map[string]any
	require.NoError(t, json.Unmarshal(body, &legacy))
	legacyInstructions, ok := legacy["instructions"].(string)
	require.True(t, ok)
	require.NotEmpty(t, legacyInstructions)
	legacyInput, ok := legacy["input"].([]any)
	require.True(t, ok)

	for _, passthrough := range []bool{false, true} {
		t.Run(fmt.Sprintf("passthrough=%v", passthrough), func(t *testing.T) {
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
			c.Request.Header.Set("Content-Type", "application/json")
			c.Request.Header.Set("User-Agent", "codex_exec/0.158.0 (Ubuntu 24.4.0; x86_64) unknown (codex_exec; 0.158.0)")
			c.Request.Header.Set("originator", "codex_exec")
			c.Request.Header.Set("version", "0.158.0")
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			upstream := &httpUpstreamRecorder{resp: &http.Response{
				StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{"text/event-stream"}},
				Body: io.NopCloser(strings.NewReader("data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_legacy\",\"output\":[],\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}}\n\ndata: [DONE]\n\n")),
			}}
			account := codexBodyEncodingTestAccount(158)
			account.Status, account.Schedulable, account.Concurrency = StatusActive, true, 1
			account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
			setCodexHTTPTestMode(account, gjson.GetBytes(body, "model").String(), false)
			svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}

			_, err := svc.Forward(context.Background(), c, account, body)
			require.NoError(t, err)
			require.Equal(t, codexExecHTTPVersion, upstream.lastReq.Header.Get("version"))
			var forwarded map[string]any
			require.NoError(t, json.Unmarshal(upstream.lastBody, &forwarded))
			require.NotContains(t, forwarded, "instructions")
			require.Equal(t, legacy["tools"], forwarded["tools"])
			forwardedInput, ok := forwarded["input"].([]any)
			require.True(t, ok)
			require.Len(t, forwardedInput, len(legacyInput)+1)
			promoted, ok := forwardedInput[0].(map[string]any)
			require.True(t, ok)
			require.Equal(t, "message", promoted["type"])
			require.Equal(t, "developer", promoted["role"])
			require.Equal(t, legacyInstructions, promoted["content"].([]any)[0].(map[string]any)["text"])
			require.Equal(t, []any{"model.base_instructions"},
				promoted["internal_chat_message_metadata_passthrough"].(map[string]any)["content_item_kinds"])
			require.Equal(t, legacyInput, forwardedInput[1:])
		})
	}
}

// Mask values only after checking they exist with the expected type. Preserve
// every key, including embedded turn metadata, so missing or added fields fail.
func maskCodexExecScopedIdentities(t *testing.T, body map[string]any) {
	t.Helper()
	mask := func(object map[string]any, fields ...string) {
		for _, field := range fields {
			value, exists := object[field]
			if !exists {
				continue
			}
			require.IsType(t, "", value, field)
			object[field] = "<scoped-" + field + ">"
		}
	}
	mask(body, "prompt_cache_key")
	metadata, ok := body["client_metadata"].(map[string]any)
	require.True(t, ok)
	mask(metadata, "session_id", "thread_id", "turn_id", "root_turn_id", "x-codex-installation-id", "x-codex-window-id")
	rawTurn, ok := metadata["x-codex-turn-metadata"].(string)
	require.True(t, ok)
	var turn map[string]any
	require.NoError(t, json.Unmarshal([]byte(rawTurn), &turn))
	mask(turn, "installation_id", "session_id", "thread_id", "turn_id", "root_turn_id", "window_id", "context_window_id")
	metadata["x-codex-turn-metadata"] = turn
}
