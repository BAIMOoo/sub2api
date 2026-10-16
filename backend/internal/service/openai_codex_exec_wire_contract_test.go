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

// Captured official Linux codex exec 0.158.0 requests use fake API-key auth
// against a loopback mock. Replay their complete JSON through both OAuth gateway
// paths, preserving fields except account-scoped identities and the explicitly
// rejected ChatGPT input metadata from upstream issue #7066.
// These fixtures prove body preservation, not OAuth TLS or compression equality.
// Do not parallelize: the test temporarily pins the process-wide identity resolver.
func TestForward_CodexExec0158NativeWireLayouts(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const userAgent = "codex_exec/0.158.0 (Ubuntu 24.4.0; x86_64) xterm-256color (codex_exec; 0.158.0)"
	codexCanonicalUAMu.RLock()
	previousResolver := codexCanonicalUAResolver
	codexCanonicalUAMu.RUnlock()
	SetCodexCanonicalUserAgentResolver(func() string { return userAgent })
	t.Cleanup(func() { SetCodexCanonicalUserAgentResolver(previousResolver) })

	for _, lite := range []bool{false, true} {
		for _, passthrough := range []bool{false, true} {
			t.Run(fmt.Sprintf("lite=%v/passthrough=%v", lite, passthrough), func(t *testing.T) {
				fixture := "standard.json"
				if lite {
					fixture = "lite.json"
				}
				body, err := os.ReadFile(filepath.Join("testdata", "codex_exec_0158", fixture))
				require.NoError(t, err)
				var request map[string]any
				require.NoError(t, json.Unmarshal(body, &request))
				c, _ := gin.CreateTestContext(httptest.NewRecorder())
				c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
				c.Request.Header.Set("Content-Type", "application/json")
				c.Request.Header.Set("User-Agent", userAgent)
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
				if lite {
					c.Request.Header.Set(responsesLiteHeader, "true")
				}
				SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
				upstream := &httpUpstreamRecorder{resp: &http.Response{
					StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{"text/event-stream"}},
					Body: io.NopCloser(strings.NewReader("data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_native\",\"output\":[],\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}}\n\ndata: [DONE]\n\n")),
				}}
				account := codexBodyEncodingTestAccount(158)
				account.Status, account.Schedulable, account.Concurrency = StatusActive, true, 1
				account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
				svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
				result, err := svc.Forward(context.Background(), c, account, body)
				require.NoError(t, err)
				require.NotNil(t, result)
				require.NotNil(t, upstream.lastReq)
				require.Equal(t, chatgptCodexURL, upstream.lastReq.URL.String())
				require.Equal(t, "codex_exec", upstream.lastReq.Header.Get("originator"))
				require.Equal(t, userAgent, upstream.lastReq.Header.Get("user-agent"))
				require.Equal(t, "0.158.0", upstream.lastReq.Header.Get("version"))
				require.Equal(t, codexRequestBodyContentEncoding, upstream.lastReq.Header.Get("content-encoding"))
				require.Equal(t, lite, isOpenAIResponsesLiteHeader(upstream.lastReq.Header.Get(responsesLiteHeader)))
				var forwarded map[string]any
				require.NoError(t, json.Unmarshal(upstream.lastBody, &forwarded))
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
				// The API-key capture contains a field ChatGPT has explicitly rejected.
				// Assert that single compatibility exception separately; never call the
				// production normalizer on expected fixtures, which could hide regressions.
				input, ok := request["input"].([]any)
				require.True(t, ok)
				forwardedInput, ok := forwarded["input"].([]any)
				require.True(t, ok)
				require.Len(t, forwardedInput, len(input))
				for i, value := range input {
					item, ok := value.(map[string]any)
					require.True(t, ok)
					delete(item, "internal_chat_message_metadata_passthrough")
					require.NotContains(t, forwardedInput[i], "internal_chat_message_metadata_passthrough")
				}
				maskCodexExecScopedIdentities(t, request)
				maskCodexExecScopedIdentities(t, forwarded)
				require.Equal(t, request, forwarded, "native request fields, prompts, tools, and item IDs must survive forwarding")
			})
		}
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
