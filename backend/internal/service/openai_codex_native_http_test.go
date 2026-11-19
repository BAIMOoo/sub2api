package service

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

const nativeHTTPTestCanonicalUA = "codex_exec/0.163.0 (Ubuntu 24.4.0; x86_64) xterm-256color (codex_exec; 0.163.0)"

func pinNativeHTTPTestIdentity(t *testing.T) {
	t.Helper()
	codexCanonicalUAMu.RLock()
	previous := codexCanonicalUAResolver
	codexCanonicalUAMu.RUnlock()
	SetCodexCanonicalUserAgentResolver(func() string { return nativeHTTPTestCanonicalUA })
	t.Cleanup(func() { SetCodexCanonicalUserAgentResolver(previous) })
}

// Synthetic IDs match the captured lengths, not the private captured values.
// Exercise Forward, including the sanitizer that runs before transform/passthrough.
func TestForward_CodexNativeHTTPToolContinuation(t *testing.T) {
	gin.SetMode(gin.TestMode)
	pinNativeHTTPTestIdentity(t)
	for _, version := range []string{"0.158.0", "0.163.0"} {
		for _, passthrough := range []bool{false, true} {
			for _, compact := range []bool{false, true} {
				for _, lite := range []bool{false, true} {
					t.Run(fmt.Sprintf("version=%s/passthrough=%v/compact=%v/lite=%v", version, passthrough, compact, lite), func(t *testing.T) {
						callID := "call_" + strings.Repeat("a", 32)
						resultID := "ctco_" + strings.Repeat("b", 36)
						input := []any{
							map[string]any{
								"type":              "reasoning",
								"id":                "rs_0672f12450da0b9c0169f07220a6c08198b68c2455ced99344",
								"encrypted_content": "gAAAAAB-native-encrypted-content",
								"summary":           []any{},
								"content": []any{
									map[string]any{"type": "reasoning_text", "text": "synthetic reasoning"},
								},
								"internal_chat_message_metadata_passthrough": map[string]any{
									"turn_id": "synthetic-turn",
								},
							},
							map[string]any{"type": "custom_tool_call", "id": "ctc_item", "call_id": callID, "name": "exec_command", "input": "pwd"},
							map[string]any{"type": "custom_tool_call_output", "id": resultID, "call_id": callID, "output": "/synthetic"},
							map[string]any{"type": "item_reference", "id": "ctc_item"},
							map[string]any{"type": "item_reference", "id": resultID},
							map[string]any{"type": "item_reference", "id": "call_previous_turn"},
						}
						body, err := json.Marshal(map[string]any{"model": "gpt-6-astra", "instructions": "Synthetic HTTP regression", "stream": !compact, "input": input})
						require.NoError(t, err)
						path := "/v1/responses"
						responseBody := "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_test\",\"output\":[],\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}}\n\ndata: [DONE]\n\n"
						contentType := "text/event-stream"
						if compact {
							path += "/compact"
							responseBody = `{"id":"cmp_test","object":"response.compaction","output":[],"usage":{"input_tokens":1,"output_tokens":1}}`
							contentType = "application/json"
						}
						c, _ := gin.CreateTestContext(httptest.NewRecorder())
						c.Request = httptest.NewRequest(http.MethodPost, path, bytes.NewReader(body))
						ua := fmt.Sprintf("codex_exec/%s (Ubuntu 24.4.0; x86_64) unknown (codex_exec; %s)", version, version)
						c.Request.Header.Set("User-Agent", ua)
						c.Request.Header.Set("originator", "codex_exec")
						c.Request.Header.Set("version", version)
						if lite {
							c.Request.Header.Set(responsesLiteHeader, "true")
						}
						SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
						upstream := &httpUpstreamRecorder{resp: &http.Response{StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{contentType}}, Body: io.NopCloser(strings.NewReader(responseBody))}}
						account := codexBodyEncodingTestAccount(158)
						account.Status, account.Schedulable, account.Concurrency = StatusActive, true, 1
						account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
						setCodexHTTPTestMode(account, "gpt-6-astra", lite)
						svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
						_, err = svc.Forward(context.Background(), c, account, body)
						require.NoError(t, err)
						require.NotNil(t, upstream.lastReq)
						assert.Equal(t, codexExecHTTPUserAgent(), upstream.lastReq.Header.Get("User-Agent"))
						assert.Equal(t, codexExecHTTPVersion, upstream.lastReq.Header.Get("version"))
						assert.Equal(t, "codex_exec", upstream.lastReq.Header.Get("originator"))
						// Compatibility may prepend a tool declaration; the history itself must survive.
						var forwarded []any
						require.NoError(t, json.Unmarshal([]byte(gjson.GetBytes(upstream.lastBody, "input").Raw), &forwarded))
						require.GreaterOrEqual(t, len(forwarded), len(input))
						assert.Equal(t, input, forwarded[len(forwarded)-len(input):])
					})
				}
			}
		}
	}
}

func TestForward_NormalizesRecognizedHarnessesToCodexExec(t *testing.T) {
	gin.SetMode(gin.TestMode)
	pinNativeHTTPTestIdentity(t)
	body, err := json.Marshal(map[string]any{
		"model": "gpt-6-astra", "stream": true, "instructions": "Synthetic harness regression",
		"tools": []any{
			map[string]any{
				"type": "function", "name": "echo", "description": "Echo input",
				"parameters": map[string]any{"type": "object", "properties": map[string]any{}},
			},
			"exec shorthand",
			map[string]any{"type": "custom"},
		},
		"tool_choice": "auto",
		"input": []any{
			map[string]any{
				"type": "reasoning", "id": "rs_tui", "encrypted_content": "opaque",
				"summary": []any{}, "content": []any{map[string]any{"type": "reasoning_text", "text": "private"}},
				"internal_chat_message_metadata_passthrough": map[string]any{"turn_id": "synthetic"},
			},
			map[string]any{"type": "web_search_call", "id": "ws_tui", "status": "completed", "action": map[string]any{"type": "search", "query": "synthetic"}},
			map[string]any{"type": "custom_tool_call", "id": "ctc_tui", "call_id": "call_tui", "name": "exec_command", "input": "pwd"},
			map[string]any{"type": "custom_tool_call_output", "id": "ctco_tui", "call_id": "call_tui", "output": "ok"},
		},
	})
	require.NoError(t, err)
	harnesses := []struct {
		name       string
		userAgent  string
		originator string
	}{
		{
			name:       "tui",
			userAgent:  "codex-tui/0.163.0 (Mac OS X 15.1.0; arm64) iTerm.app (codex-tui; 0.163.0)",
			originator: "codex-tui",
		},
		{
			name:       "desktop",
			userAgent:  "Codex Desktop/0.163.0 (Mac OS X 15.1.0; arm64) unknown (Codex Desktop; 0.163.0)",
			originator: "Codex Desktop",
		},
		{
			name:       "ide",
			userAgent:  "codex_vscode/0.163.0 (Mac OS X 15.1.0; arm64) vscode (codex_vscode; 0.163.0)",
			originator: "codex_vscode",
		},
		{
			name:       "tui originator override",
			userAgent:  "cccc/0.163.0 (Mac OS X 15.1.0; arm64) iTerm.app (codex-tui; 0.163.0)",
			originator: "cccc",
		},
	}
	for _, harness := range harnesses {
		for _, passthrough := range []bool{false, true} {
			for _, lite := range []bool{false, true} {
				t.Run(fmt.Sprintf("%s/passthrough=%v/lite=%v", harness.name, passthrough, lite), func(t *testing.T) {
					c, _ := gin.CreateTestContext(httptest.NewRecorder())
					c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
					c.Request.Header.Set("User-Agent", harness.userAgent)
					c.Request.Header.Set("originator", harness.originator)
					c.Request.Header.Set("version", "0.163.0")
					if lite {
						c.Request.Header.Set(responsesLiteHeader, "true")
					}
					SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
					upstream := &httpUpstreamRecorder{resp: &http.Response{
						StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{"text/event-stream"}},
						Body: io.NopCloser(strings.NewReader("data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_harness\",\"output\":[],\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}}\n\ndata: [DONE]\n\n")),
					}}
					account := codexBodyEncodingTestAccount(158)
					account.Status, account.Schedulable, account.Concurrency = StatusActive, true, 1
					account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
					setCodexHTTPTestMode(account, "gpt-6-astra", lite)
					svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
					_, err := svc.Forward(context.Background(), c, account, body)
					require.NoError(t, err)
					require.Equal(t, "codex_exec", upstream.lastReq.Header.Get("originator"))
					require.Equal(t, codexExecHTTPVersion, upstream.lastReq.Header.Get("version"))
					require.Equal(t, codexExecHTTPUserAgent(), upstream.lastReq.Header.Get("user-agent"))
					var forwarded map[string]any
					require.NoError(t, json.Unmarshal(upstream.lastBody, &forwarded))
					items, ok := forwarded["input"].([]any)
					require.True(t, ok)
					var reasoning map[string]any
					var toolCall, toolOutput map[string]any
					for _, raw := range items {
						item, ok := raw.(map[string]any)
						if !ok {
							continue
						}
						switch item["type"] {
						case "reasoning":
							reasoning = item
						case "custom_tool_call":
							toolCall = item
						case "custom_tool_call_output":
							toolOutput = item
						}
					}
					require.NotNil(t, reasoning)
					require.NotNil(t, toolCall)
					require.NotNil(t, toolOutput)
					require.Equal(t, "rs_tui", reasoning["id"])
					require.Contains(t, reasoning, "content")
					require.Contains(t, reasoning, "internal_chat_message_metadata_passthrough")
					require.Equal(t, "ctc_tui", toolCall["id"])
					require.Equal(t, "call_tui", toolCall["call_id"])
					require.Equal(t, "ctco_tui", toolOutput["id"])
					require.Equal(t, "call_tui", toolOutput["call_id"])
					require.Equal(t, "auto", forwarded["tool_choice"])
					if lite {
						require.NotEmpty(t, items)
						carrier, ok := items[0].(map[string]any)
						require.True(t, ok)
						require.Equal(t, "additional_tools", carrier["type"])
						require.NotContains(t, forwarded, "instructions")
						require.NotContains(t, forwarded, "tools")
						developer, ok := items[1].(map[string]any)
						require.True(t, ok)
						require.Equal(t, "message", developer["type"])
						require.Equal(t, "developer", developer["role"])
						content, ok := developer["content"].([]any)
						require.True(t, ok)
						part, ok := content[0].(map[string]any)
						require.True(t, ok)
						require.Equal(t, "Synthetic harness regression", part["text"])
						metadata, ok := developer["internal_chat_message_metadata_passthrough"].(map[string]any)
						require.True(t, ok)
						require.Equal(t, []any{"model.base_instructions"}, metadata["content_item_kinds"])
						carrierTools, ok := carrier["tools"].([]any)
						require.True(t, ok)
						var names []string
						for _, rawTool := range carrierTools {
							tool, ok := rawTool.(map[string]any)
							require.True(t, ok)
							toolType, ok := tool["type"].(string)
							require.True(t, ok)
							names = append(names, toolType)
						}
						require.NotContains(t, names, "web_search")
						require.Contains(t, names, "namespace")
						require.Contains(t, names, "custom")
						require.NotContains(t, names, "function")
						var namespaceTools []any
						for _, rawTool := range carrierTools {
							tool, ok := rawTool.(map[string]any)
							require.True(t, ok)
							if tool["type"] == "namespace" {
								namespaceTools, ok = tool["tools"].([]any)
								require.True(t, ok)
								break
							}
						}
						require.NotNil(t, namespaceTools)
						var namespaceTypes []string
						for _, rawTool := range namespaceTools {
							tool, ok := rawTool.(map[string]any)
							require.True(t, ok)
							toolType, ok := tool["type"].(string)
							require.True(t, ok)
							namespaceTypes = append(namespaceTypes, toolType)
						}
						require.ElementsMatch(t, []string{"function", "custom"}, namespaceTypes)
						return
					}
					require.Equal(t, "Synthetic harness regression", forwarded["instructions"])
					tools, ok := forwarded["tools"].([]any)
					require.True(t, ok)
					var names []string
					for _, raw := range tools {
						if _, ok := raw.(string); ok {
							names = append(names, "custom")
							continue
						}
						tool, ok := raw.(map[string]any)
						require.True(t, ok)
						toolType, ok := tool["type"].(string)
						require.True(t, ok)
						names = append(names, toolType)
					}
					require.ElementsMatch(t, []string{"function", "custom", "custom"}, names)
					for _, raw := range items {
						item, ok := raw.(map[string]any)
						require.True(t, ok)
						require.NotEqual(t, "additional_tools", item["type"])
					}
				})
			}
		}
	}
}

func TestCodexNativeHTTPIdentityBoundaries(t *testing.T) {
	gin.SetMode(gin.TestMode)
	pinNativeHTTPTestIdentity(t)
	const nativeUA = "codex_exec/0.158.0 (Ubuntu 24.4.0; x86_64) unknown (codex_exec; 0.158.0)"
	const overrideUA = "codex-tui/0.150.0 (Mac OS X 15.1.0; arm64) iTerm.app (codex-tui; 0.150.0)"
	for _, tc := range []struct {
		name, ua, originator, version, override string
		force, ws, duplicate, preserve          bool
	}{
		{name: "native", ua: nativeUA, originator: "codex_exec", version: "0.158.0", preserve: true},
		{name: "missing_version", ua: nativeUA, originator: "codex_exec", preserve: true},
		{name: "missing_originator", ua: nativeUA, version: "0.158.0", preserve: true},
		{name: "missing_ua", originator: "codex_exec", version: "0.158.0"},
		{name: "third_party", ua: "third-party/1.0", originator: "codex_exec", version: "0.158.0"},
		{name: "mismatched_originator", ua: nativeUA, originator: "codex-tui", version: "0.158.0"},
		{name: "mismatched_version", ua: nativeUA, originator: "codex_exec", version: "0.163.0"},
		{name: "invalid_version", ua: nativeUA, originator: "codex_exec", version: "invalid"},
		{name: "old_version", ua: strings.ReplaceAll(nativeUA, "0.158.0", "0.100.0"), originator: "codex_exec", version: "0.100.0"},
		{name: "mismatched_trailer", ua: strings.Replace(nativeUA, "; 0.158.0)", "; 0.150.0)", 1), originator: "codex_exec", version: "0.158.0"},
		{name: "control_byte", ua: nativeUA + "\r\n", originator: "codex_exec", version: "0.158.0"},
		{name: "duplicate_version", ua: nativeUA, originator: "codex_exec", version: "0.158.0", duplicate: true},
		{name: "force", ua: nativeUA, originator: "codex_exec", version: "0.158.0", force: true},
		{name: "account_override", ua: nativeUA, originator: "codex_exec", version: "0.158.0", override: overrideUA},
		{name: "force_overrides_account", ua: nativeUA, originator: "codex_exec", version: "0.158.0", override: overrideUA, force: true},
		{name: "ws_unchanged", ua: nativeUA, originator: "codex_exec", version: "0.158.0", ws: true},
	} {
		for _, passthrough := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/passthrough=%v", tc.name, passthrough), func(t *testing.T) {
				c, _ := gin.CreateTestContext(httptest.NewRecorder())
				c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
				c.Request.Header.Set("User-Agent", tc.ua)
				c.Request.Header.Set("originator", tc.originator)
				c.Request.Header.Set("version", tc.version)
				if tc.duplicate {
					c.Request.Header.Add("version", "0.163.0")
				}
				transport := OpenAIClientTransportHTTP
				if tc.ws {
					transport = OpenAIClientTransportWS
				}
				SetOpenAIClientTransport(c, transport)
				account := codexBodyEncodingTestAccount(158)
				account.Credentials["user_agent"] = tc.override
				svc := &OpenAIGatewayService{cfg: &config.Config{Gateway: config.GatewayConfig{ForceCodexCLI: tc.force}}}
				body := []byte(`{"model":"gpt-6-astra","instructions":"test","input":[],"stream":true}`)
				var req *http.Request
				var err error
				if passthrough {
					req, err = svc.buildUpstreamRequestOpenAIPassthrough(context.Background(), c, account, body, "test-token")
				} else {
					req, err = svc.buildUpstreamRequest(context.Background(), c, account, body, "test-token", true, "", true)
				}
				require.NoError(t, err)
				expectedUA, expectedVersion, expectedOriginator := codexExecHTTPUserAgent(), codexExecHTTPVersion, "codex_exec"
				if tc.ws {
					expectedUA, expectedVersion = nativeHTTPTestCanonicalUA, "0.163.0"
				}
				if tc.ws && tc.preserve {
					expectedUA, expectedVersion = nativeUA, "0.158.0"
				} else if tc.ws && tc.override != "" && !tc.force {
					expectedUA = strings.ReplaceAll(overrideUA, "0.150.0", "0.163.0")
					expectedOriginator = "codex-tui"
				}
				assert.Equal(t, expectedUA, req.Header.Get("User-Agent"))
				assert.Equal(t, expectedVersion, req.Header.Get("version"))
				assert.Equal(t, expectedOriginator, req.Header.Get("originator"))
			})
		}
	}
}

func TestCodexHTTPPolicyNormalizesNativeHarnesses(t *testing.T) {
	for _, tc := range []struct {
		name, ua, originator string
		expected             codexHTTPPolicy
	}{
		{
			name:       "codex_exec",
			ua:         "codex_exec/0.163.0 (Ubuntu 24.4.0; x86_64) xterm-256color (codex_exec; 0.163.0)",
			originator: "codex_exec",
			expected:   codexHTTPExec,
		},
		{
			name:       "codex_tui",
			ua:         "codex-tui/0.163.0 (Mac OS X 15.1.0; arm64) iTerm.app (codex-tui; 0.163.0)",
			originator: "codex-tui",
			expected:   codexHTTPExec,
		},
		{
			name:       "codex_desktop",
			ua:         "Codex Desktop/0.163.0 (Mac OS X 15.1.0; arm64) unknown (Codex Desktop; 0.163.0)",
			originator: "Codex Desktop",
			expected:   codexHTTPExec,
		},
		{
			name:       "codex_ide",
			ua:         "codex_vscode/0.163.0 (Mac OS X 15.1.0; arm64) vscode (codex_vscode; 0.163.0)",
			originator: "codex_vscode",
			expected:   codexHTTPExec,
		},
		{
			name:       "tui originator override",
			ua:         "cccc/0.163.0 (Mac OS X 15.1.0; arm64) iTerm.app (codex-tui; 0.163.0)",
			originator: "cccc",
			expected:   codexHTTPExec,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
			c.Request.Header.Set("User-Agent", tc.ua)
			c.Request.Header.Set("originator", tc.originator)
			c.Request.Header.Set("version", "0.163.0")
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			assert.Equal(t, tc.expected, codexHTTPPolicyForRequest(c, codexBodyEncodingTestAccount(158)))
		})
	}
}

func TestCodexHTTPPolicyKeepsAPIKeyTransportSeparate(t *testing.T) {
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	c.Request.Header.Set("User-Agent", "codex-tui/0.163.0 (Mac OS X 15.1.0; arm64) iTerm.app (codex-tui; 0.163.0)")
	c.Request.Header.Set("originator", "codex-tui")
	c.Request.Header.Set("version", "0.163.0")
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	account := codexBodyEncodingTestAccount(158)
	account.Type = AccountTypeAPIKey
	assert.Equal(t, codexHTTPLegacy, codexHTTPPolicyForRequest(c, account))
}

func TestCodexNativeHTTPInputIDBoundsAndLegacyDefaults(t *testing.T) {
	for _, native := range []bool{false, true} {
		t.Run(fmt.Sprintf("native=%v", native), func(t *testing.T) {
			callID := "call_" + strings.Repeat("a", 70)
			input := []any{
				map[string]any{"type": "custom_tool_call", "call_id": callID, "name": "exec_command", "input": "pwd"},
				map[string]any{"type": "custom_tool_call_output", "call_id": callID, "id": "ctco_valid", "output": "ok"},
				map[string]any{"type": "custom_tool_call_output", "call_id": "call_other", "id": strings.Repeat("x", 65), "output": "ok"},
				map[string]any{"type": "custom_tool_call_output", "call_id": "call_empty", "id": "", "output": "ok"},
				map[string]any{"type": "item_reference", "id": "call_previous"},
			}
			body, err := json.Marshal(map[string]any{"input": input})
			require.NoError(t, err)
			sanitized, _, err := sanitizeOpenAIResponsesInputItemIDsWithOptions(body, openAIResponsesInputIDOptions{NativeCodexHTTP: native})
			require.NoError(t, err)
			assert.Equal(t, native, gjson.GetBytes(sanitized, "input.1.id").Exists())
			assert.Equal(t, native, gjson.GetBytes(sanitized, "input.2.id").Exists())
			assert.Equal(t, native, gjson.GetBytes(sanitized, "input.3.id").Exists())
			policy := codexHTTPLegacy
			if native {
				policy = codexHTTPExec
			}
			filtered := filterCodexInputWithOptions(input, codexInputFilterOptions{
				PreserveReferences: true, PreserveCallIDs: true, HTTPPolicy: policy,
			})
			callItem, ok := filtered[0].(map[string]any)
			require.True(t, ok)
			call := callItem["call_id"]
			output, ok := filtered[1].(map[string]any)
			require.True(t, ok)
			assert.Equal(t, call, output["call_id"])
			callString, ok := call.(string)
			require.True(t, ok)
			if native {
				assert.Greater(t, len(callString), 64)
			} else {
				assert.LessOrEqual(t, len(callString), 64)
			}
			_, hasID := output["id"]
			assert.Equal(t, native, hasID)
			if native {
				assert.Contains(t, filtered[2], "id")
				assert.Contains(t, filtered[3], "id")
			} else {
				assert.NotContains(t, filtered[2], "id")
				assert.NotContains(t, filtered[3], "id")
			}
			ref := "fc_previous"
			if native {
				ref = "call_previous"
			}
			reference, ok := filtered[4].(map[string]any)
			require.True(t, ok)
			assert.Equal(t, ref, reference["id"])
		})
	}
}

func TestCodexNativeHTTPPreservesReasoningItemExactly(t *testing.T) {
	input := []any{
		map[string]any{
			"type":              "reasoning",
			"id":                "rs_0672f12450da0b9c0169f07220a6c08198b68c2455ced99344",
			"encrypted_content": "gAAAAAB-enc-payload",
			"summary":           []any{},
		},
	}

	native := filterCodexInputWithOptions(input, codexInputFilterOptions{
		PreserveReferences: true,
		HTTPPolicy:         codexHTTPExec,
	})
	require.Equal(t, input, native)

	legacy := filterCodexInputWithOptions(input, codexInputFilterOptions{
		PreserveReferences: true,
	})
	legacyReasoning, ok := legacy[0].(map[string]any)
	require.True(t, ok)
	require.NotContains(t, legacyReasoning, "id")
	require.Equal(t, "gAAAAAB-enc-payload", legacyReasoning["encrypted_content"])
}
