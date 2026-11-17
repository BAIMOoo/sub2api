package service

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func setCodexHTTPTestMode(account *Account, model string, lite bool) {
	account.SetUpstreamModelMetadataSnapshot(UpstreamModelMetadataSnapshot{
		Models: map[string]UpstreamModelMetadata{
			model: {ID: model, CodexToolCapabilities: map[string]json.RawMessage{
				"use_responses_lite": json.RawMessage(strconv.FormatBool(lite)),
			}},
		},
	})
}

func TestCodexHTTPEgress_FixedContractOverridesLegacyControls(t *testing.T) {
	gin.SetMode(gin.TestMode)
	pinNativeHTTPTestIdentity(t) // Server auto-sync reports 0.163; HTTP must remain 0.158.
	for _, passthrough := range []bool{false, true} {
		t.Run(strconv.FormatBool(passthrough), func(t *testing.T) {
			logs, restore := captureStructuredLog(t)
			defer restore()
			body := []byte(`{"model":"gpt-5.5","instructions":"KEEP TASK","temperature":0.2,"max_output_tokens":64,"stop":"END","tools":[{"type":"function","name":"read_file","description":"Read","parameters":{"type":"object"}}],"input":[{"type":"web_search_call","id":"ws_old","status":"completed","action":{"type":"search","query":"prior search"}},{"role":"user","content":"KEEP HISTORY"}]}`)
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
			c.Request.Header.Set("User-Agent", "ordinary-agent/1.0")
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			account := codexBodyEncodingTestAccount(1500)
			account.Extra = map[string]any{"openai_oauth_passthrough": passthrough, "codex_cli_only": true}
			upstream := captureCodexHTTPEgress(t, c, account, body, "responses")
			require.Equal(t, "0.158.0", upstream.lastReq.Header.Get("version"))
			require.True(t, strings.HasPrefix(upstream.lastReq.Header.Get("User-Agent"), "codex_exec/0.158.0 "))
			for _, field := range []string{"temperature", "max_output_tokens", "stop"} {
				require.False(t, gjson.GetBytes(upstream.lastBody, field).Exists(), field)
				require.True(t, logs.ContainsFieldValue("ignored_parameters", field), field)
			}
			require.Equal(t, int64(1), gjson.GetBytes(upstream.lastBody, "tools.#").Int())
			require.Equal(t, "read_file", gjson.GetBytes(upstream.lastBody, "tools.0.name").String())
			require.Equal(t, "KEEP TASK", gjson.GetBytes(upstream.lastBody, "instructions").String())
			require.Equal(t, "ws_old", gjson.GetBytes(upstream.lastBody, "input.0.id").String())
			require.Contains(t, string(upstream.lastBody), "KEEP HISTORY")
		})
	}
}

func TestCodexHTTPEgress_RejectsUnknownChatHistoryBeforeDecoding(t *testing.T) {
	for _, path := range []string{"/v1/chat/completions", "/v1/responses"} {
		recorder := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(recorder)
		body := []byte(`{"model":"gpt-5.5","messages":[{"role":"assistant","content":null,"audio":{"id":"audio_keep"}}]}`)
		c.Request = httptest.NewRequest(http.MethodPost, path, bytes.NewReader(body))
		SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
		upstream := &httpUpstreamRecorder{}
		svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
		account := codexBodyEncodingTestAccount(2000)
		var err error
		if strings.HasSuffix(path, "/chat/completions") {
			_, err = svc.ForwardAsChatCompletions(context.Background(), c, account, body, "", "")
		} else {
			_, err = svc.Forward(context.Background(), c, account, body)
		}
		require.Error(t, err)
		require.Nil(t, upstream.lastReq)
		require.Equal(t, 400, recorder.Code)
		require.Equal(t, "messages[0].audio", gjson.GetBytes(recorder.Body.Bytes(), "error.param").String())
	}
}

func TestCodexHTTPEgress_MessagesSchemaPreservesLargeInteger(t *testing.T) {
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	body := []byte(`{"model":"gpt-5.5","max_tokens":16,"messages":[{"role":"user","content":"keep schema"}],"tools":[{"name":"lookup","description":"Lookup ID","input_schema":{"type":"object","properties":{"id":{"type":"integer","const":9007199254740993}}}}]}`)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", bytes.NewReader(body))
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	upstream := captureCodexHTTPEgress(t, c, codexBodyEncodingTestAccount(2001), body, "messages")
	require.Equal(t, "9007199254740993", gjson.GetBytes(upstream.lastBody, "tools.0.parameters.properties.id.const").Raw)
}

func TestCodexHTTPEgress_EmptyLegacyToolResultAndUnknownModelEnvelope(t *testing.T) {
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	body := []byte(`{"model":"unknown-agent-model","input":[{"type":"function_call","name":"lookup","call_id":"call_lookup","arguments":"{}"},{"role":"tool","tool_call_id":"call_lookup","content":""}]}`)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	upstream := captureCodexHTTPEgress(t, c, codexBodyEncodingTestAccount(2002), body, "responses")
	output := gjson.GetBytes(upstream.lastBody, "input.1.output")
	require.True(t, output.Exists())
	require.Equal(t, "", output.String())
	require.Equal(t, "reasoning.encrypted_content", gjson.GetBytes(upstream.lastBody, "include.0").String())
}

func TestCodexHTTPEgress_RejectsUnrepresentableRawFields(t *testing.T) {
	for _, tc := range []struct{ path, body string }{
		{"/v1/chat/completions", `{"model":"gpt-5.5","messages":[],"tools":[{"type":"function","function":{"name":"lookup","defer_loading":true,"parameters":{"type":"object"}}}]}`},
		{"/v1/messages", `{"model":"gpt-5.5","messages":[],"context_management":{"edits":[]}}`},
		{"/v1/chat/completions", `{"model":"gpt-5.5","messages":"not-an-array"}`},
		{"/v1/messages", `{"model":"gpt-5.5","messages":"not-an-array"}`},
		{"/v1/responses", `{"model":"gpt-5.5","input":[{"type":"additional_tools","role":"developer","tools":[],"content":"KEEP TASK"}]}`},
	} {
		recorder := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(recorder)
		body := []byte(tc.body)
		c.Request = httptest.NewRequest(http.MethodPost, tc.path, bytes.NewReader(body))
		SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
		upstream := &httpUpstreamRecorder{}
		svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
		account := codexBodyEncodingTestAccount(2100)
		var err error
		switch tc.path {
		case "/v1/chat/completions":
			_, err = svc.ForwardAsChatCompletions(context.Background(), c, account, body, "", "")
		case "/v1/messages":
			_, err = svc.ForwardAsAnthropic(context.Background(), c, account, body, "", "")
		default:
			_, err = svc.Forward(context.Background(), c, account, body)
		}
		require.Error(t, err, tc.path)
		require.Equal(t, 400, recorder.Code, tc.body)
		require.Nil(t, upstream.lastReq)
	}
}

func TestCodexHTTPEgress_ArbitrarySessionSeedUsesStableV7(t *testing.T) {
	body := []byte(`{"model":"gpt-5.5","prompt_cache_key":"harness-session-123","input":[]}`)
	var previous string
	for range 2 {
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
		SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
		upstream := captureCodexHTTPEgress(t, c, codexBodyEncodingTestAccount(2101), body, "responses")
		session := upstream.lastReq.Header.Get("session-id")
		parsed, err := uuid.Parse(session)
		require.NoError(t, err)
		require.Equal(t, uuid.Version(7), parsed.Version())
		require.Equal(t, session, upstream.lastReq.Header.Get("thread-id"))
		require.Equal(t, session, gjson.GetBytes(upstream.lastBody, "prompt_cache_key").String())
		require.Equal(t, session+":0", upstream.lastReq.Header.Get("x-codex-window-id"))
		if previous != "" {
			require.Equal(t, previous, session)
		}
		previous = session
	}
}

func TestCodexHTTPEgress_AccountAndTransportBoundaries(t *testing.T) {
	gin.SetMode(gin.TestMode)
	pinNativeHTTPTestIdentity(t)
	for _, tc := range []struct {
		name, platform, accountType, authMode string
		transport                             OpenAIClientTransport
		aligned                               bool
	}{
		{name: "oauth", platform: PlatformOpenAI, accountType: AccountTypeOAuth, transport: OpenAIClientTransportHTTP, aligned: true},
		{name: "pat", platform: PlatformOpenAI, accountType: AccountTypeOAuth, authMode: "personal_access_token", transport: OpenAIClientTransportHTTP, aligned: true},
		{name: "setup token", platform: PlatformOpenAI, accountType: AccountTypeSetupToken, transport: OpenAIClientTransportHTTP, aligned: true},
		{name: "API key", platform: PlatformOpenAI, accountType: AccountTypeAPIKey, transport: OpenAIClientTransportHTTP},
		{name: "other OAuth", platform: PlatformGrok, accountType: AccountTypeOAuth, transport: OpenAIClientTransportHTTP},
		{name: "unknown ingress", platform: PlatformOpenAI, accountType: AccountTypeOAuth},
		{name: "WS bridge", platform: PlatformOpenAI, accountType: AccountTypeOAuth, transport: OpenAIClientTransportWS},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
			SetOpenAIClientTransport(c, tc.transport)
			account := codexBodyEncodingTestAccount(1100)
			account.Platform, account.Type = tc.platform, tc.accountType
			account.Credentials["openai_auth_mode"] = tc.authMode
			require.Equal(t, tc.aligned, codexHTTPPolicyForRequest(c, account).preservesCodexExec())
			body := []byte(`{"model":"gpt-6-astra","instructions":"keep","input":[],"tools":[{"type":"function","name":"echo","description":"Echo","parameters":{"type":"object"}}]}`)
			normalized, mode, err := normalizeCodexHTTPBody(c, account, body)
			require.NoError(t, err)
			if tc.aligned {
				require.True(t, mode.lite)
				require.True(t, mode.verified)
			} else {
				require.Equal(t, body, normalized)
			}
		})
	}
}

func TestCodexHTTPEgress_LiteInputToStandard(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, passthrough := range []bool{false, true} {
		t.Run(strconv.FormatBool(passthrough), func(t *testing.T) {
			body := []byte(`{"model":"gpt-5.5","input":[{"type":"additional_tools","role":"developer","tools":[{"type":"namespace","name":"functions","description":"agent tools","tools":[{"type":"function","name":"read_file","description":"Read file","parameters":{"type":"object"}}]}]},{"type":"message","role":"developer","content":[{"type":"input_text","text":"AGENT INSTRUCTIONS"}],"internal_chat_message_metadata_passthrough":{"content_item_kinds":["model.base_instructions"]}},{"type":"function_call","name":"read_file","namespace":"functions","call_id":"call_agent","arguments":"{}"},{"type":"function_call_output","call_id":"call_agent","output":"KEEP OUTPUT"}],"tool_choice":{"type":"function","name":"read_file","namespace":"functions"}}`)
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
			c.Request.Header.Set(responsesLiteHeader, "true")
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			account := codexBodyEncodingTestAccount(1200)
			account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
			upstream := captureCodexHTTPEgress(t, c, account, body, "responses")
			require.Empty(t, upstream.lastReq.Header.Get(responsesLiteHeader))
			require.False(t, gjson.GetBytes(upstream.lastBody, `input.#(type=="additional_tools")`).Exists())
			require.Equal(t, "read_file", gjson.GetBytes(upstream.lastBody, "tools.0.tools.0.name").String())
			require.Equal(t, "functions", gjson.GetBytes(upstream.lastBody, "tool_choice.namespace").String())
			require.Equal(t, "functions", gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call").namespace`).String())
			require.Equal(t, 1, strings.Count(string(upstream.lastBody), "AGENT INSTRUCTIONS"))
			require.Contains(t, string(upstream.lastBody), "KEEP OUTPUT")
			require.Equal(t, "true", c.GetHeader(responsesLiteHeader))
		})
	}
}

func TestCodexHTTPEgress_ServerIdentityCannotSwitchHarness(t *testing.T) {
	pinNativeHTTPTestIdentity(t)
	SetCodexCanonicalUserAgentResolver(func() string {
		return "codex-tui/0.163.0 (Mac OS X 15.1.0; arm64) iTerm.app (codex-tui; 0.163.0)"
	})
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	h := make(http.Header)
	(&OpenAIGatewayService{}).applyCodexHTTPIdentityHeaders(c, codexBodyEncodingTestAccount(1), h)
	require.Equal(t, "codex_exec", h.Get("originator"))
	require.Equal(t, codexExecHTTPVersion, h.Get("version"))
	require.Equal(t, codexExecHTTPUserAgent(), h.Get("User-Agent"))
}

func TestCodexHTTPEgress_WSHTTPBuildersRetainLegacyContract(t *testing.T) {
	gin.SetMode(gin.TestMode)
	pinNativeHTTPTestIdentity(t)
	for _, passthrough := range []bool{false, true} {
		t.Run(strconv.FormatBool(passthrough), func(t *testing.T) {
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
			SetOpenAIClientTransport(c, OpenAIClientTransportWS)
			account := codexBodyEncodingTestAccount(1300)
			const override = "codex-tui/0.163.0 (Mac OS X 15.1.0; arm64) iTerm.app (codex-tui; 0.163.0)"
			account.Credentials["user_agent"] = override
			body := []byte(`{"model":"gpt-6-astra","instructions":"WS bridge keeps its layout","input":[],"tools":[{"type":"function","name":"echo","description":"Echo","parameters":{"type":"object"}}]}`)
			svc := &OpenAIGatewayService{cfg: &config.Config{}}
			var request *http.Request
			var err error
			if passthrough {
				request, err = svc.buildUpstreamRequestOpenAIPassthrough(context.Background(), c, account, body, "synthetic-token")
			} else {
				request, err = svc.buildUpstreamRequest(context.Background(), c, account, body, "synthetic-token", true, "", true)
			}
			require.NoError(t, err)
			wire, err := io.ReadAll(request.Body)
			require.NoError(t, err)
			require.NoError(t, request.Body.Close())
			require.JSONEq(t, string(body), string(readCodexUpstreamBody(request.Header, wire)))
			require.Empty(t, request.Header.Get(responsesLiteHeader))
			require.Equal(t, override, request.Header.Get("User-Agent"))
			require.Equal(t, "codex-tui", request.Header.Get("originator"))
		})
	}
}

func TestCodexHTTPEgress_InvalidLiteParameterReturns400(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, passthrough := range []bool{false, true} {
		for _, field := range []string{"parallel_tool_calls", "reasoning"} {
			t.Run(fmt.Sprintf("passthrough=%v/%s", passthrough, field), func(t *testing.T) {
				recorder := httptest.NewRecorder()
				c, _ := gin.CreateTestContext(recorder)
				body := []byte(fmt.Sprintf(`{"model":"gpt-6-astra","input":[],"%s":"invalid"}`, field))
				c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
				SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
				account := codexBodyEncodingTestAccount(1400)
				account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
				upstream := &httpUpstreamRecorder{}
				svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
				_, err := svc.Forward(context.Background(), c, account, body)
				require.Error(t, err)
				require.Nil(t, upstream.lastReq)
				require.Equal(t, http.StatusBadRequest, recorder.Code)
				require.Equal(t, field, gjson.GetBytes(recorder.Body.Bytes(), "error.param").String())
				require.Equal(t, "invalid_request_error", gjson.GetBytes(recorder.Body.Bytes(), "error.type").String())
			})
		}
	}
}

// Exercise public forwarding methods and inspect the actual decompressed wire
// request. A deliberate upstream 400 stops response conversion after capture.
func captureCodexHTTPEgress(t *testing.T, c *gin.Context, account *Account, body []byte, route string) *httpUpstreamRecorder {
	t.Helper()
	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusBadRequest,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(`{"error":{"type":"invalid_request_error","message":"synthetic capture complete"}}`)),
	}}
	svc := &OpenAIGatewayService{cfg: &config.Config{Gateway: config.GatewayConfig{
		ForcedCodexInstructionsTemplate: "REPLACE THE AGENT PROMPT",
	}}, httpUpstream: upstream}
	var err error
	switch route {
	case "chat", "responses-shaped-chat":
		_, err = svc.ForwardAsChatCompletions(context.Background(), c, account, body, "", "")
	case "messages":
		_, err = svc.ForwardAsAnthropic(context.Background(), c, account, body, "", "")
	default:
		_, err = svc.Forward(context.Background(), c, account, body)
	}
	require.Error(t, err)
	require.NotNil(t, upstream.lastReq, "request must reach the synthetic upstream: %v", err)
	return upstream
}

func TestCodexHTTPEgress_PreservesAgentAcrossEntrypoints(t *testing.T) {
	gin.SetMode(gin.TestMode)
	pinNativeHTTPTestIdentity(t)
	previousEnforcement := codexIdentityEnforcement.Load()
	SetCodexIdentityEnforcementEnabled(false)
	t.Cleanup(func() { SetCodexIdentityEnforcementEnabled(previousEnforcement) })
	for _, route := range []string{"responses", "passthrough", "chat", "responses-shaped-chat", "messages", "compact", "compact-passthrough"} {
		for _, model := range []string{"gpt-5.5", "gpt-6-astra", "gpt-5.3-codex-spark"} {
			t.Run(route+"/"+model, func(t *testing.T) {
				path := "/v1/responses"
				request := map[string]any{
					"model": model, "instructions": "AGENT SYSTEM KEEP EXACTLY", "stream": true,
					"tools": []any{map[string]any{"type": "function", "name": "read_file", "description": "Read agent file", "parameters": map[string]any{"type": "object", "properties": map[string]any{"path": map[string]any{"type": "string"}}}}},
					"input": []any{
						map[string]any{"role": "user", "content": "AGENT TASK"},
						map[string]any{"type": "function_call", "id": "fc_item", "call_id": "call_agent", "name": "read_file", "arguments": `{"path":"README.md"}`},
						map[string]any{"type": "function_call_output", "call_id": "call_agent", "output": "AGENT FILE CONTENT"},
					},
				}
				if route == "chat" {
					path = "/v1/chat/completions"
					delete(request, "instructions")
					delete(request, "input")
					request["tools"] = []any{map[string]any{"type": "function", "function": map[string]any{"name": "read_file", "description": "Read agent file", "parameters": map[string]any{"type": "object", "properties": map[string]any{"path": map[string]any{"type": "string"}}}}}}
					request["messages"] = []any{
						map[string]any{"role": "system", "content": "AGENT SYSTEM KEEP EXACTLY"},
						map[string]any{"role": "user", "content": "AGENT TASK"},
						map[string]any{"role": "assistant", "tool_calls": []any{map[string]any{"id": "call_agent", "type": "function", "function": map[string]any{"name": "read_file", "arguments": `{"path":"README.md"}`}}}},
						map[string]any{"role": "tool", "tool_call_id": "call_agent", "content": "AGENT FILE CONTENT"},
					}
				} else if route == "messages" {
					path = "/v1/messages"
					delete(request, "instructions")
					delete(request, "input")
					request["system"] = "AGENT SYSTEM KEEP EXACTLY"
					request["max_tokens"] = 100
					request["tools"] = []any{map[string]any{"name": "read_file", "description": "Read agent file", "input_schema": map[string]any{"type": "object", "properties": map[string]any{"path": map[string]any{"type": "string"}}}}}
					request["messages"] = []any{
						map[string]any{"role": "user", "content": "AGENT TASK"},
						map[string]any{"role": "assistant", "content": []any{map[string]any{"type": "tool_use", "id": "call_agent", "name": "read_file", "input": map[string]any{"path": "README.md"}}}},
						map[string]any{"role": "user", "content": []any{map[string]any{"type": "tool_result", "tool_use_id": "call_agent", "content": "AGENT FILE CONTENT"}}},
					}
				} else if strings.HasPrefix(route, "compact") {
					path += "/compact"
				}
				body, err := json.Marshal(request)
				require.NoError(t, err)
				c, _ := gin.CreateTestContext(httptest.NewRecorder())
				c.Request = httptest.NewRequest(http.MethodPost, path, bytes.NewReader(body))
				c.Request.Header.Set("User-Agent", "arbitrary-harness/2")
				SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
				account := codexBodyEncodingTestAccount(901)
				account.Credentials["user_agent"] = "codex-tui/0.150.0 (Mac OS X 15.1.0; arm64) iTerm.app (codex-tui; 0.150.0)"
				account.Extra = map[string]any{"openai_oauth_passthrough": strings.Contains(route, "passthrough")}
				upstream := captureCodexHTTPEgress(t, c, account, body, route)
				require.Equal(t, codexExecHTTPUserAgent(), upstream.lastReq.Header.Get("User-Agent"))
				require.Equal(t, codexExecHTTPVersion, upstream.lastReq.Header.Get("version"))
				require.Equal(t, "codex_exec", upstream.lastReq.Header.Get("originator"))
				require.NotContains(t, upstream.lastReq.Header.Get("OpenAI-Beta"), "responses=experimental")
				wire := string(upstream.lastBody)
				for _, content := range []string{"AGENT SYSTEM KEEP EXACTLY", "AGENT TASK", "AGENT FILE CONTENT"} {
					require.Equal(t, 1, strings.Count(wire, content), content)
				}
				require.NotContains(t, wire, "REPLACE THE AGENT PROMPT")
				require.NotContains(t, wire, "<sub2api-")
				call := gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call")`)
				output := gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call_output")`)
				require.Equal(t, "call_agent", call.Get("call_id").String())
				require.Equal(t, call.Get("call_id").String(), output.Get("call_id").String())
				require.Equal(t, "read_file", call.Get("name").String())
				require.JSONEq(t, `{"path":"README.md"}`, call.Get("arguments").String())
				lite := model == "gpt-6-astra"
				require.Equal(t, lite, isOpenAIResponsesLiteHeader(upstream.lastReq.Header.Get(responsesLiteHeader)))
				if lite {
					require.False(t, gjson.GetBytes(upstream.lastBody, "tools").Exists())
					require.False(t, gjson.GetBytes(upstream.lastBody, "instructions").Exists())
					require.Equal(t, "read_file", gjson.GetBytes(upstream.lastBody, "input.0.tools.0.tools.0.name").String())
					require.Equal(t, "string", gjson.GetBytes(upstream.lastBody, "input.0.tools.0.tools.0.parameters.properties.path.type").String())
					require.Equal(t, "all_turns", gjson.GetBytes(upstream.lastBody, "reasoning.context").String())
					require.False(t, gjson.GetBytes(upstream.lastBody, "parallel_tool_calls").Bool())
				} else if !strings.HasPrefix(route, "compact") {
					require.Equal(t, "read_file", gjson.GetBytes(upstream.lastBody, "tools.0.name").String())
				}
				if strings.HasPrefix(route, "compact") {
					require.True(t, gjson.GetBytes(upstream.lastBody, "stream").Bool())
					require.False(t, gjson.GetBytes(upstream.lastBody, "store").Bool())
					require.Equal(t, chatgptCodexURL, upstream.lastReq.URL.String())
					require.True(t, HasCompactionTriggerInInput(upstream.lastBody))
				}
			})
		}
	}
}

func TestCodexHTTPEgress_ModeSelectionAndFailover(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, passthrough := range []bool{false, true} {
		for _, declared := range []bool{false, true} {
			t.Run(fmt.Sprintf("passthrough=%v/declared=%v", passthrough, declared), func(t *testing.T) {
				logSink, restore := captureStructuredLog(t)
				defer restore()
				body := []byte(`{"model":"agent-model","instructions":"PRIVATE AGENT PROMPT","input":[{"role":"user","content":"task"}],"tools":[{"type":"function","name":"read_file","description":"Read file","parameters":{"type":"object","properties":{}}}]}`)
				c, _ := gin.CreateTestContext(httptest.NewRecorder())
				c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
				if declared {
					c.Request.Header.Set(responsesLiteHeader, "true")
				}
				SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
				originalHeader := c.Request.Header.Clone()
				originalBody := bytes.Clone(body)
				for i, capability := range []string{"true", "false", "null", ""} {
					account := codexBodyEncodingTestAccount(int64(1000 + i))
					account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
					account.Credentials["model_mapping"] = map[string]any{"agent-model": "final-model"}
					finalModel := "final-model"
					if passthrough { // Passthrough intentionally retains the channel's model.
						finalModel = "agent-model"
					}
					if capability != "" {
						account.SetUpstreamModelMetadataSnapshot(UpstreamModelMetadataSnapshot{Models: map[string]UpstreamModelMetadata{
							finalModel: {CodexToolCapabilities: map[string]json.RawMessage{"use_responses_lite": json.RawMessage(capability)}},
						}})
					}
					upstream := captureCodexHTTPEgress(t, c, account, body, "responses")
					wantLite := capability == "true" || (capability == "null" || capability == "") && declared
					require.Equal(t, wantLite, isOpenAIResponsesLiteHeader(upstream.lastReq.Header.Get(responsesLiteHeader)))
					require.Equal(t, finalModel, gjson.GetBytes(upstream.lastBody, "model").String())
					require.Equal(t, wantLite, gjson.GetBytes(upstream.lastBody, `input.#(type=="additional_tools")`).Exists())
					require.Equal(t, originalHeader, c.Request.Header)
					require.Equal(t, originalBody, body)
				}
				require.True(t, logSink.ContainsFieldValue("alignment_status", "unverified"))
				source := "standard_fallback"
				if declared {
					source = "request_fallback"
				}
				require.True(t, logSink.ContainsFieldValue("mode_source", source))
				require.False(t, logSink.ContainsFieldValue("request_body", "PRIVATE AGENT PROMPT"))
			})
		}
	}
}
