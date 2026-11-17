package service

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestCodexHTTPOrderedHeadersMatch0158Reference(t *testing.T) {
	for _, name := range []string{"standard", "lite"} {
		t.Run(name, func(t *testing.T) {
			root := filepath.Join("testdata", "codex_exec_0158_linux_amd64")
			body, err := os.ReadFile(filepath.Join(root, name+".json"))
			require.NoError(t, err)
			rawHeaders, err := os.ReadFile(filepath.Join(root, "http", name+".headers.json"))
			require.NoError(t, err)
			var reference struct {
				Headers [][2]string `json:"headers"`
				Order   []string    `json:"header_order"`
			}
			require.NoError(t, json.Unmarshal(rawHeaders, &reference))
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
			for _, pair := range reference.Headers {
				c.Request.Header.Set(pair[0], pair[1])
			}
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			account := codexBodyEncodingTestAccount(1900)
			svc := &OpenAIGatewayService{cfg: &config.Config{}}
			req, err := svc.buildUpstreamRequest(context.Background(), c, account, body, "synthetic-token", true, gjson.GetBytes(body, "prompt_cache_key").String(), false)
			require.NoError(t, err)
			writer, reader := net.Pipe()
			t.Cleanup(func() { _ = writer.Close(); _ = reader.Close() })
			received := make(chan []byte, 1)
			go func() { wire, _ := io.ReadAll(reader); received <- wire }()
			ordered := tlsfingerprint.NewOrderedWriteConn(writer, tlsfingerprint.OpenAICodexLinuxProfile().HeaderOrders()...)
			require.NoError(t, req.Write(ordered))
			require.NoError(t, ordered.Close())
			wire := <-received
			headerBlock, _, ok := bytes.Cut(wire, []byte("\r\n\r\n"))
			require.True(t, ok)
			lines := strings.Split(string(headerBlock), "\r\n")
			require.Equal(t, "POST /backend-api/codex/responses HTTP/1.1", lines[0])
			var actual []string
			for _, line := range lines[1:] {
				key, _, ok := strings.Cut(line, ":")
				require.True(t, ok)
				actual = append(actual, key)
			}
			require.Equal(t, reference.Order, actual)
		})
	}
}

// Captured official Linux codex exec 0.158.0 requests use fake API-key auth
// against a loopback mock. Replay their complete JSON through both OAuth gateway
// paths, preserving fields except account-scoped identities.
// These fixtures prove body preservation, not OAuth TLS or compression equality.
// Do not parallelize: the test temporarily pins the process-wide identity resolver.
func TestForward_CodexExec0158NativeWireLayouts(t *testing.T) {
	testForwardCodexExecWireLayouts(t, "codex_exec_0158")
}

func TestForward_CodexExec0158LinuxAMD64WireLayouts(t *testing.T) {
	testForwardCodexExecWireLayouts(t, "codex_exec_0158_linux_amd64")
}

func testForwardCodexExecWireLayouts(t *testing.T, fixtureDirectory string) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	const userAgent = "codex_exec/0.158.0 (Ubuntu 24.4.0; x86_64) unknown (codex_exec; 0.158.0)"
	codexCanonicalUAMu.RLock()
	previousResolver := codexCanonicalUAResolver
	codexCanonicalUAMu.RUnlock()
	SetCodexCanonicalUserAgentResolver(func() string {
		return "codex_exec/0.163.0 (Ubuntu 24.4.0; x86_64) xterm-256color (codex_exec; 0.163.0)"
	})
	t.Cleanup(func() { SetCodexCanonicalUserAgentResolver(previousResolver) })

	for _, lite := range []bool{false, true} {
		for _, passthrough := range []bool{false, true} {
			t.Run(fmt.Sprintf("lite=%v/passthrough=%v", lite, passthrough), func(t *testing.T) {
				fixture := "standard.json"
				if lite {
					fixture = "lite.json"
				}
				body, err := os.ReadFile(filepath.Join("testdata", fixtureDirectory, fixture))
				require.NoError(t, err)
				var request map[string]any
				require.NoError(t, json.Unmarshal(body, &request))
				c, _ := gin.CreateTestContext(httptest.NewRecorder())
				c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
				c.Request.Header.Set("Content-Type", "application/json")
				c.Request.Header.Set("User-Agent", userAgent)
				c.Request.Header.Set("originator", "codex_exec")
				c.Request.Header.Set("version", "0.158.0")
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
				require.Equal(t, codexExecHTTPUserAgent(), upstream.lastReq.Header.Get("user-agent"))
				require.Equal(t, codexExecHTTPVersion, upstream.lastReq.Header.Get("version"))
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
				input, ok := request["input"].([]any)
				require.True(t, ok)
				forwardedInput, ok := forwarded["input"].([]any)
				require.True(t, ok)
				require.Len(t, forwardedInput, len(input))
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
