package service

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestCodexHTTPIngressLogsIgnoredGenerationControlsWithoutChangingTask(t *testing.T) {
	gin.SetMode(gin.TestMode)
	logs, restore := captureStructuredLog(t)
	defer restore()

	body := []byte(`{"model":"gpt-5.5","instructions":"KEEP TASK","temperature":0.2,"max_output_tokens":64,"stop":"END","input":[{"role":"user","content":"KEEP HISTORY"}],"tools":[{"type":"function","name":"read_file","description":"Read","parameters":{"type":"object"}}]}`)
	c, recorder := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
	c.Request.Header.Set("User-Agent", "ordinary-agent/1.0")
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	account := codexBodyEncodingTestAccount(2000)
	account.Extra = map[string]any{"openai_oauth_passthrough": false, "codex_cli_only": true}
	upstream := captureCodexHTTPForward(t, c, account, body, &config.Config{
		Gateway: config.GatewayConfig{ForcedCodexInstructionsTemplate: "REPLACE THE AGENT PROMPT"},
	})

	require.NotNil(t, recorder)
	require.False(t, gjson.GetBytes(upstream.lastBody, "instructions").Exists())
	require.Equal(t, "developer", gjson.GetBytes(upstream.lastBody, "input.0.role").String())
	require.Equal(t, "KEEP TASK", gjson.GetBytes(upstream.lastBody, "input.0.content.0.text").String())
	require.Contains(t, string(upstream.lastBody), "KEEP HISTORY")
	require.NotContains(t, string(upstream.lastBody), "REPLACE THE AGENT PROMPT")
	require.Equal(t, "read_file", gjson.GetBytes(upstream.lastBody, "tools.0.name").String())
	for _, field := range []string{"temperature", "max_output_tokens", "stop"} {
		require.False(t, gjson.GetBytes(upstream.lastBody, field).Exists(), field)
		require.True(t, logs.ContainsFieldValue("ignored_parameters", field), field)
	}
	require.False(t, logs.ContainsFieldValue("request_body", "KEEP TASK"))
}

func TestCodexHTTPIngressRejectsAmbiguousTaskContent(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, test := range []struct {
		name string
		body string
	}{
		{name: "commands", body: `{"model":"gpt-5.5","commands":"must not disappear"}`},
		{name: "prompt conflicts with input", body: `{"model":"gpt-5.5","prompt":"legacy","input":[{"role":"user","content":"native"}]}`},
		{name: "input conflicts with messages", body: `{"model":"gpt-5.5","input":[{"role":"user","content":"native"}],"messages":[{"role":"user","content":"legacy"}]}`},
		{name: "invalid messages shape", body: `{"model":"gpt-5.5","messages":{"role":"user","content":"legacy"}}`},
		{name: "invalid legacy instructions type", body: `{"model":"gpt-5.5","messages":[{"role":"user","content":"legacy"}],"instructions":123}`},
		{name: "tools conflict with functions", body: `{"model":"gpt-5.5","input":[{"role":"user","content":"hi"}],"tools":[],"functions":[{"name":"lookup"}]}`},
		{name: "empty base64 image", body: `{"model":"gpt-5.5","input":[{"role":"user","content":[{"type":"input_image","image_url":"data:image/png;base64,"}]}]}`},
	} {
		t.Run(test.name, func(t *testing.T) {
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader([]byte(test.body)))
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			upstream := &httpUpstreamRecorder{resp: newCodexHTTPSyntheticErrorResponse()}
			svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}

			_, err := svc.Forward(context.Background(), c, codexBodyEncodingTestAccount(2001), []byte(test.body))
			require.Error(t, err)
			require.Nil(t, upstream.lastReq)
			require.Equal(t, http.StatusBadRequest, recorder.Code)
			require.Equal(t, "invalid_request_error", gjson.GetBytes(recorder.Body.Bytes(), "error.type").String())
		})
	}
}

func TestCodexHTTPResponsesPreservesEmptyLegacyToolResult(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.5",
		"tools":[{"type":"web_search"}],
		"tool_choice":{"type":"web_search"},
		"messages":[
			{"role":"user","content":"Use the tool"},
			{"role":"assistant","tool_calls":[{"id":"call_legacy","type":"function","function":{"name":"lookup","arguments":"{}"}}]},
			{"role":"tool","tool_call_id":"call_legacy","content":""}
		]
	}`)
	for _, passthrough := range []bool{false, true} {
		t.Run("passthrough="+boolString(passthrough), func(t *testing.T) {
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			account := codexBodyEncodingTestAccount(2105)
			account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
			upstream := captureCodexHTTPForward(t, c, account, body, &config.Config{})

			require.False(t, gjson.GetBytes(upstream.lastBody, "messages").Exists())
			require.JSONEq(t, `{"type":"web_search"}`, gjson.GetBytes(upstream.lastBody, "tool_choice").Raw)
			output := gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call_output")`)
			require.Equal(t, "call_legacy", output.Get("call_id").String())
			require.Equal(t, `""`, output.Get("output").Raw)
		})
	}
}

func TestCodexHTTPResponsesDoesNotBroadenExplicitToolChoice(t *testing.T) {
	for _, test := range []struct {
		name       string
		selection  string
		wantChoice string
		wantParam  string
	}{
		{
			name:       "native named selection without a matching declaration",
			selection:  `"tool_choice":{"type":"function","name":"lookup"}`,
			wantChoice: `{"type":"function","name":"lookup"}`,
		},
		{
			name:       "native hosted selection without a matching declaration",
			selection:  `"tool_choice":{"type":"web_search"}`,
			wantChoice: `{"type":"web_search"}`,
		},
		{
			name:       "legacy nested selection",
			selection:  `"tool_choice":{"type":"function","function":{"name":"available"}}`,
			wantChoice: `{"type":"function","name":"available"}`,
		},
		{
			name:       "legacy function call",
			selection:  `"function_call":{"name":"available"}`,
			wantChoice: `{"type":"function","name":"available"}`,
		},
		{
			name:      "legacy fields cannot disappear",
			selection: `"tool_choice":{"type":"function","function":{"name":"available","description":"keep"}}`,
			wantParam: "tool_choice",
		},
		{
			name:      "legacy function call fields cannot disappear",
			selection: `"function_call":{"name":"available","description":"keep"}`,
			wantParam: "function_call",
		},
		{
			name:      "conflicting formats",
			selection: `"tool_choice":"none","function_call":{"name":"available"}`,
			wantParam: "tool_choice",
		},
	} {
		for _, passthrough := range []bool{false, true} {
			t.Run(test.name+"/passthrough="+boolString(passthrough), func(t *testing.T) {
				body := []byte(`{"model":"gpt-5.5","input":"continue","tools":[{"type":"function","name":"available","description":"Available","parameters":{"type":"object"}}],` + test.selection + `}`)
				rec := httptest.NewRecorder()
				c, _ := gin.CreateTestContext(rec)
				c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
				SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
				account := codexBodyEncodingTestAccount(2106)
				account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
				upstream := &httpUpstreamRecorder{resp: newCodexHTTPSyntheticErrorResponse()}
				svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}

				_, err := svc.Forward(context.Background(), c, account, body)
				require.Error(t, err)
				if test.wantParam != "" {
					require.Nil(t, upstream.lastReq)
					require.Equal(t, http.StatusBadRequest, rec.Code)
					require.Equal(t, "invalid_request_error", gjson.GetBytes(rec.Body.Bytes(), "error.type").String())
					require.Equal(t, test.wantParam, gjson.GetBytes(rec.Body.Bytes(), "error.param").String())
					return
				}
				require.NotNil(t, upstream.lastReq, "request must reach the synthetic upstream: %v", err)
				require.Equal(t, "available", gjson.GetBytes(upstream.lastBody, "tools.0.name").String())
				require.JSONEq(t, test.wantChoice, gjson.GetBytes(upstream.lastBody, "tool_choice").Raw)
				require.False(t, gjson.GetBytes(upstream.lastBody, "function_call").Exists())
			})
		}
	}
}

func TestCodexHTTPChatPreservesAgentSemantics(t *testing.T) {
	for _, lite := range []bool{false, true} {
		t.Run("lite="+boolString(lite), func(t *testing.T) {
			body := []byte(`{"model":"gpt-5.5","messages":[{"role":"system","content":"KEEP CHAT SYSTEM"},{"role":"user","content":[{"type":"text","text":"KEEP CHAT TASK"}]},{"role":"assistant","content":"","tool_calls":[{"id":"call_chat","type":"function","function":{"name":"read_file","arguments":"{\"path\":\"README.md\"}"}}]},{"role":"tool","tool_call_id":"call_chat","content":""}],"metadata":{"caller":"KEEP CHAT METADATA"},"tools":[{"type":"function","function":{"name":"read_file","description":"Read","parameters":{"type":"object","properties":{"id":{"type":"integer","const":9007199254740993}}}}}],"tool_choice":{"type":"function","function":{"name":"read_file"}},"stream":false}`)
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewReader(body))
			c.Request.Header.Set("User-Agent", "arbitrary-chat-harness/3")
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			account := codexBodyEncodingTestAccount(2100)
			setCodexHTTPTestMode(account, "gpt-5.5", lite)
			upstream := &httpUpstreamRecorder{resp: newCodexHTTPSyntheticErrorResponse()}
			svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}

			_, err := svc.ForwardAsChatCompletions(context.Background(), c, account, body, "", "")
			require.Error(t, err)
			require.NotNil(t, upstream.lastReq)
			require.Equal(t, codexExecHTTPVersion, upstream.lastReq.Header.Get("version"))
			require.False(t, gjson.GetBytes(upstream.lastBody, "instructions").Exists())
			require.Contains(t, string(upstream.lastBody), "KEEP CHAT SYSTEM")
			require.Contains(t, string(upstream.lastBody), "KEEP CHAT TASK")
			require.Contains(t, string(upstream.lastBody), "KEEP CHAT METADATA")
			require.Equal(t, "call_chat", gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call").call_id`).String())
			require.Equal(t, "call_chat", gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call_output").call_id`).String())
			require.Equal(t, "", gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call_output").output`).String())
			require.Contains(t, string(upstream.lastBody), "9007199254740993")
		})
	}

	t.Run("lite_without_tools_omits_empty_carrier", func(t *testing.T) {
		body := []byte(`{"model":"gpt-6.2-sol","messages":[{"role":"system","content":"KEEP CHAT SYSTEM"},{"role":"user","content":"KEEP CHAT TASK"}],"stream":false}`)
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewReader(body))
		SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
		account := codexBodyEncodingTestAccount(2107)
		setCodexHTTPTestMode(account, "gpt-6.2-sol", true)
		upstream := &httpUpstreamRecorder{resp: newCodexHTTPSyntheticErrorResponse()}
		svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}

		_, err := svc.ForwardAsChatCompletions(context.Background(), c, account, body, "", "")
		require.Error(t, err)
		require.NotNil(t, upstream.lastReq)
		require.False(t, gjson.GetBytes(upstream.lastBody, "instructions").Exists())
		require.False(t, gjson.GetBytes(upstream.lastBody, "tools").Exists())
		require.False(t, gjson.GetBytes(upstream.lastBody, `input.#(type=="additional_tools")`).Exists())
		require.Equal(t, "developer", gjson.GetBytes(upstream.lastBody, "input.0.role").String())
		require.Contains(t, gjson.GetBytes(upstream.lastBody, "input.0").Raw, "KEEP CHAT SYSTEM")
	})
}

func TestCodexHTTPChatRejectsUnrepresentableFieldsBeforeUpstream(t *testing.T) {
	for _, test := range []struct {
		name string
		body string
	}{
		{
			name: "unknown message field",
			body: `{"model":"gpt-5.5","messages":[{"role":"assistant","content":null,"audio":{"id":"keep"}}]}`,
		},
		{
			name: "unknown tool field",
			body: `{"model":"gpt-5.5","messages":[{"role":"user","content":"hi"}],"tools":[{"type":"function","function":{"name":"read_file","parameters":{"type":"object"},"defer_loading":true}}]}`,
		},
		{
			name: "unsupported content part",
			body: `{"model":"gpt-5.5","messages":[{"role":"user","content":[{"type":"audio","audio":{"id":"keep"}}]}]}`,
		},
		{
			name: "duplicate call id",
			body: `{"model":"gpt-5.5","messages":[{"role":"assistant","content":"","tool_calls":[{"id":"call_1","type":"function","function":{"name":"one","arguments":"{}"}},{"id":"call_1","type":"function","function":{"name":"two","arguments":"{}"}}]}]}`,
		},
		{
			name: "orphan legacy result",
			body: `{"model":"gpt-5.5","messages":[{"role":"function","name":"lookup","content":"result"}]}`,
		},
		{
			name: "stream options",
			body: `{"model":"gpt-5.5","messages":[{"role":"user","content":"hi"}],"stream_options":{"include_usage":true}}`,
		},
		{
			name: "invalid instructions type",
			body: `{"model":"gpt-5.5","messages":[{"role":"user","content":"hi"}],"instructions":123}`,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewReader([]byte(test.body)))
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			upstream := &httpUpstreamRecorder{resp: newCodexHTTPSyntheticErrorResponse()}
			svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}

			_, err := svc.ForwardAsChatCompletions(
				context.Background(),
				c,
				codexBodyEncodingTestAccount(2101),
				[]byte(test.body),
				"",
				"",
			)
			require.Error(t, err)
			require.Nil(t, upstream.lastReq)
			require.Equal(t, http.StatusBadRequest, recorder.Code)
			require.Equal(t, "invalid_request_error", gjson.GetBytes(recorder.Body.Bytes(), "error.type").String())
		})
	}
}

func TestCodexHTTPChatPreservesHostedTools(t *testing.T) {
	body := []byte(`{"model":"gpt-5.5","messages":[{"role":"user","content":"search"}],"tools":[{"type":"web_search"},{"type":"code_execution"},{"type":"x_search","allowed_x_handles":["openai"],"enable_image_understanding":true}]}`)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewReader(body))
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	account := codexBodyEncodingTestAccount(2104)
	setCodexHTTPTestMode(account, "gpt-5.5", false)
	upstream := &httpUpstreamRecorder{resp: newCodexHTTPSyntheticErrorResponse()}
	svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}

	_, err := svc.ForwardAsChatCompletions(context.Background(), c, account, body, "", "")
	require.Error(t, err)
	require.NotNil(t, upstream.lastReq)
	require.Equal(t, "web_search", gjson.GetBytes(upstream.lastBody, "tools.0.type").String())
	require.Equal(t, "code_execution", gjson.GetBytes(upstream.lastBody, "tools.1.type").String())
	require.Equal(t, "x_search", gjson.GetBytes(upstream.lastBody, "tools.2.type").String())
	require.Equal(t, "openai", gjson.GetBytes(upstream.lastBody, "tools.2.allowed_x_handles.0").String())
	require.True(t, gjson.GetBytes(upstream.lastBody, "tools.2.enable_image_understanding").Bool())
}

func TestCodexHTTPChatLogsIgnoredGenerationControls(t *testing.T) {
	logs, restore := captureStructuredLog(t)
	defer restore()
	body := []byte(`{"model":"gpt-5.5","messages":[{"role":"user","content":"keep task"}],"max_tokens":32,"temperature":0.1,"stop":"END"}`)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewReader(body))
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	account := codexBodyEncodingTestAccount(2103)
	setCodexHTTPTestMode(account, "gpt-5.5", false)
	upstream := &httpUpstreamRecorder{resp: newCodexHTTPSyntheticErrorResponse()}
	svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}

	_, err := svc.ForwardAsChatCompletions(context.Background(), c, account, body, "", "")
	require.Error(t, err)
	require.NotNil(t, upstream.lastReq)
	for _, field := range []string{"max_tokens", "temperature", "stop"} {
		require.False(t, gjson.GetBytes(upstream.lastBody, field).Exists())
		require.True(t, logs.ContainsFieldValue("ignored_parameters", field))
	}
	require.Contains(t, string(upstream.lastBody), "keep task")
}

func TestCodexHTTPChatPreservesResponsesShapedBody(t *testing.T) {
	body := []byte(`{"model":"gpt-5.5","previous_response_id":"resp_keep","metadata":{"caller":"KEEP RESPONSES SHAPE"},"input":[{"type":"message","role":"user","content":"KEEP SHAPED TASK"},{"type":"function_call_output","call_id":"call_prev","output":"KEEP SHAPED RESULT"}],"tools":[{"type":"function","name":"read_file","description":"Read","parameters":{"type":"object"}}],"stream":false}`)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/chat/completions", bytes.NewReader(body))
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	account := codexBodyEncodingTestAccount(2102)
	setCodexHTTPTestMode(account, "gpt-5.5", false)
	upstream := &httpUpstreamRecorder{resp: newCodexHTTPSyntheticErrorResponse()}
	svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}

	_, err := svc.ForwardAsChatCompletions(context.Background(), c, account, body, "", "")
	require.Error(t, err)
	require.NotNil(t, upstream.lastReq)
	require.Contains(t, string(upstream.lastBody), "KEEP RESPONSES SHAPE")
	require.Contains(t, string(upstream.lastBody), "KEEP SHAPED TASK")
	require.Contains(t, string(upstream.lastBody), "KEEP SHAPED RESULT")
	require.Equal(t, "resp_keep", gjson.GetBytes(upstream.lastBody, "previous_response_id").String())
}

func TestCodexHTTPForwardPreservesAgentTaskAndToolContinuation(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, passthrough := range []bool{false, true} {
		for _, lite := range []bool{false, true} {
			t.Run("passthrough="+boolString(passthrough)+"/lite="+boolString(lite), func(t *testing.T) {
				body := []byte(`{"model":"gpt-5.5","stream":false,"instructions":"AGENT SYSTEM KEEP EXACTLY","previous_response_id":"resp_keep","metadata":{"caller":"KEEP METADATA"},"opaque_task":{"state":"KEEP OPAQUE"},"tools":[{"type":"function","name":"read_file","description":"Read agent file","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":null}}],"tool_choice":{"type":"function","name":"read_file"},"input":[{"type":"message","role":"user","content":"AGENT TASK"},{"type":"reasoning","id":"rs_keep","encrypted_content":"opaque-reasoning","summary":[],"content":[{"type":"reasoning_text","text":"KEEP REASONING"}],"internal_chat_message_metadata_passthrough":{"turn_id":"private-turn"}},{"type":"function_call","id":"fc_item","call_id":"call_agent","name":"read_file","arguments":"{\"path\":\"README.md\"}"},{"type":"function_call_output","id":"fco_item","call_id":"call_agent","output":"AGENT FILE CONTENT"},{"type":"item_reference","id":"fc_item"}]}`)
				originalBody := bytes.Clone(body)
				c, _ := gin.CreateTestContext(httptest.NewRecorder())
				c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
				c.Request.Header.Set("User-Agent", "arbitrary-harness/2")
				originalHeaders := c.Request.Header.Clone()
				SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
				account := codexBodyEncodingTestAccount(2002)
				account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
				setCodexHTTPTestMode(account, "gpt-5.5", lite)

				upstream := captureCodexHTTPForward(t, c, account, body, &config.Config{})
				require.Equal(t, originalBody, body)
				require.Equal(t, originalHeaders, c.Request.Header)

				wire := string(upstream.lastBody)
				for _, value := range []string{
					"AGENT SYSTEM KEEP EXACTLY",
					"AGENT TASK",
					"KEEP REASONING",
					"AGENT FILE CONTENT",
					"private-turn",
					"KEEP METADATA",
					"KEEP OPAQUE",
				} {
					require.Contains(t, wire, value)
				}
				require.Equal(t, "call_agent", gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call").call_id`).String())
				require.Equal(t, "read_file", gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call").name`).String())
				require.JSONEq(t, `{"path":"README.md"}`, gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call").arguments`).String())
				require.Equal(t, "call_agent", gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call_output").call_id`).String())
				require.Equal(t, "fc_item", gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call").id`).String())
				require.Equal(t, "fco_item", gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call_output").id`).String())
				require.Equal(t, "rs_keep", gjson.GetBytes(upstream.lastBody, `input.#(type=="reasoning").id`).String())
				require.Equal(t, "resp_keep", gjson.GetBytes(upstream.lastBody, "previous_response_id").String())
				require.Contains(t, wire, `"required":null`)
				require.True(t, gjson.GetBytes(upstream.lastBody, "stream").Bool())
				require.False(t, gjson.GetBytes(upstream.lastBody, "store").Bool())
				require.Equal(t, lite, isOpenAIResponsesLiteHeader(upstream.lastReq.Header.Get(responsesLiteHeader)))
				require.False(t, gjson.GetBytes(upstream.lastBody, "instructions").Exists())
				if lite {
					require.False(t, gjson.GetBytes(upstream.lastBody, "tools").Exists())
					require.Equal(t, "additional_tools", gjson.GetBytes(upstream.lastBody, "input.0.type").String())
					require.Equal(t, "developer", gjson.GetBytes(upstream.lastBody, "input.1.role").String())
					require.Equal(t, "AGENT SYSTEM KEEP EXACTLY", gjson.GetBytes(upstream.lastBody, "input.1.content.0.text").String())
				} else {
					require.Equal(t, "read_file", gjson.GetBytes(upstream.lastBody, "tools.0.name").String())
					require.Equal(t, "developer", gjson.GetBytes(upstream.lastBody, "input.0.role").String())
					require.Equal(t, "AGENT SYSTEM KEEP EXACTLY", gjson.GetBytes(upstream.lastBody, "input.0.content.0.text").String())
				}

				session := upstream.lastReq.Header.Get("session-id")
				require.NotEmpty(t, session)
				parsed, err := uuid.Parse(session)
				require.NoError(t, err)
				require.Equal(t, uuid.Version(7), parsed.Version())
				require.Equal(t, session, gjson.GetBytes(upstream.lastBody, "prompt_cache_key").String())
				require.Equal(t, session, gjson.GetBytes(upstream.lastBody, "client_metadata.session_id").String())
				require.Equal(t, session, upstream.lastReq.Header.Get("thread-id"))
				require.Equal(t, session, upstream.lastReq.Header.Get("x-client-request-id"))
				require.NotEmpty(t, upstream.lastReq.Header.Get("x-codex-turn-metadata"))
			})
		}
	}
}

func TestCodexHTTPIngressRejectsUnrepresentableToolSemanticsBeforeUpstream(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, test := range []struct {
		name string
		body string
	}{
		{
			name: "function tool without name",
			body: `{"model":"gpt-5.5","tools":[{"type":"function","description":"Read","parameters":{"type":"object"}}],"input":[{"role":"user","content":"hi"}]}`,
		},
		{
			name: "tool result without call id",
			body: `{"model":"gpt-5.5","input":[{"role":"tool","content":"KEEP RESULT"}]}`,
		},
		{
			name: "tool result with non-text content",
			body: `{"model":"gpt-5.5","input":[{"role":"tool","tool_call_id":"call_a","content":{"opaque":true}}]}`,
		},
		{
			name: "tool call without name",
			body: `{"model":"gpt-5.5","input":[{"type":"function_call","call_id":"call_a","arguments":"{}"}]}`,
		},
		{
			name: "orphan tool output",
			body: `{"model":"gpt-5.5","input":[{"type":"function_call_output","call_id":"missing_call","output":"KEEP RESULT"}]}`,
		},
		{
			name: "non-string text part",
			body: `{"model":"gpt-5.5","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":{"opaque":true}}]}]}`,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader([]byte(test.body)))
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			upstream := &httpUpstreamRecorder{resp: newCodexHTTPSyntheticErrorResponse()}
			svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}

			_, err := svc.Forward(context.Background(), c, codexBodyEncodingTestAccount(2003), []byte(test.body))
			require.Error(t, err)
			require.Nil(t, upstream.lastReq)
			require.Equal(t, http.StatusBadRequest, recorder.Code)
		})
	}
}

func TestCodexHTTPForwardPreservesLargeNumbersInOpaqueToolSchema(t *testing.T) {
	body := []byte(`{"model":"gpt-5.5","tools":[{"type":"function","name":"lookup","description":"Lookup","parameters":{"type":"object","properties":{"id":{"type":"integer","const":9007199254740993}}}}],"input":[{"role":"user","content":"lookup"}]}`)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	upstream := captureCodexHTTPForward(t, c, codexBodyEncodingTestAccount(2004), body, &config.Config{})
	var request map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(upstream.lastBody, &request))
	require.Equal(t, "9007199254740993", gjson.GetBytes(upstream.lastBody, "tools.0.parameters.properties.id.const").Raw)
}

func TestCodexHTTPPreservesNamespacesDespiteLegacyFlattenOverride(t *testing.T) {
	for _, passthrough := range []bool{false, true} {
		for _, lite := range []bool{false, true} {
			t.Run("passthrough="+boolString(passthrough)+"/lite="+boolString(lite), func(t *testing.T) {
				body := []byte(`{"model":"gpt-5.5","tools":[{"type":"namespace","name":"functions","description":"Agent tools","tools":[{"type":"function","name":"read_file","description":"Read","parameters":{"type":"object"}}]}],"input":[{"type":"function_call","id":"fc_ns","call_id":"call_ns","namespace":"functions","name":"read_file","arguments":"{}"}]}`)
				c, _ := gin.CreateTestContext(httptest.NewRecorder())
				c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
				SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
				account := codexBodyEncodingTestAccount(2011)
				account.Extra = map[string]any{
					"openai_oauth_passthrough":            passthrough,
					"openai_responses_flatten_namespaces": true,
				}
				setCodexHTTPTestMode(account, "gpt-5.5", lite)

				upstream := captureCodexHTTPForward(t, c, account, body, &config.Config{})
				require.Contains(t, string(upstream.lastBody), `"type":"namespace"`)
				require.Contains(t, string(upstream.lastBody), `"name":"functions"`)
				require.Equal(
					t,
					"functions",
					gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call").namespace`).String(),
				)
			})
		}
	}
}

func TestCodexHTTPPreservesExplicitImageToolDespiteLegacyAccountPolicy(t *testing.T) {
	for _, passthrough := range []bool{false, true} {
		t.Run("passthrough="+boolString(passthrough), func(t *testing.T) {
			body := []byte(`{"model":"gpt-5.5","stream":false,"input":[{"role":"user","content":"draw"}],"tools":[{"type":"image_generation","format":"jpeg"},{"type":"function","name":"shell","parameters":{"type":"object"}}]}`)
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
			c.Request.Header.Set("User-Agent", "codex_exec/0.151.0 (Linux; x86_64)")
			c.Set("api_key", &APIKey{Group: &Group{AllowImageGeneration: true}})
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			account := codexBodyEncodingTestAccount(2012)
			account.Extra = map[string]any{
				"openai_oauth_passthrough":                       passthrough,
				featureKeyCodexImageGenerationExplicitToolPolicy: codexImageGenerationExplicitToolPolicyStrip,
			}
			setCodexHTTPTestMode(account, "gpt-5.5", false)

			upstream := captureCodexHTTPForward(t, c, account, body, &config.Config{})
			require.True(t, gjson.GetBytes(upstream.lastBody, `tools.#(type=="image_generation")`).Exists())
		})
	}
}

func TestCodexHTTPPreservesOpaqueItemAndCallIDs(t *testing.T) {
	callID := "call_" + strings.Repeat("x", 80)
	itemID := "opaque-item-" + strings.Repeat("y", 80)
	outputID := "opaque-output-" + strings.Repeat("z", 80)
	body := []byte(`{"model":"gpt-5.5","input":[{"type":"function_call","id":"` + itemID + `","call_id":"` + callID + `","name":"read_file","arguments":"{}"},{"type":"function_call_output","id":"` + outputID + `","call_id":"` + callID + `","output":"KEEP"}]}`)
	for _, passthrough := range []bool{false, true} {
		t.Run("passthrough="+boolString(passthrough), func(t *testing.T) {
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			account := codexBodyEncodingTestAccount(2013)
			account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
			setCodexHTTPTestMode(account, "gpt-5.5", false)

			upstream := captureCodexHTTPForward(t, c, account, body, &config.Config{})
			call := gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call")`)
			output := gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call_output")`)
			require.Equal(t, itemID, call.Get("id").String())
			require.Equal(t, callID, call.Get("call_id").String())
			require.Equal(t, outputID, output.Get("id").String())
			require.Equal(t, callID, output.Get("call_id").String())
		})
	}
}

func TestCodexHTTPAcceptsLosslessCrossTurnToolOutputs(t *testing.T) {
	for _, body := range [][]byte{
		[]byte(`{"model":"gpt-5.5","input":[{"type":"item_reference","id":"call_previous"},{"type":"function_call_output","call_id":"call_previous","output":"KEEP"}]}`),
		[]byte(`{"model":"gpt-5.5","input":[{"type":"function_call_output","name":"task_result","output":"KEEP EXTERNAL TASK"}]}`),
	} {
		c, _ := gin.CreateTestContext(httptest.NewRecorder())
		c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
		SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
		account := codexBodyEncodingTestAccount(2014)
		setCodexHTTPTestMode(account, "gpt-5.5", false)

		upstream := captureCodexHTTPForward(t, c, account, body, &config.Config{})
		require.Contains(t, string(upstream.lastBody), "KEEP")
	}
}

func TestCodexHTTPDoesNotConsumeWSInvalidEncryptedContentLineage(t *testing.T) {
	body := []byte(`{"model":"gpt-5.5","prompt_cache_key":"lineage-session","input":[{"type":"reasoning","id":"rs_keep","encrypted_content":"stale-cipher","summary":[]}]}`)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
	groupID := int64(42)
	c.Set("api_key", &APIKey{ID: 24, GroupID: &groupID, Group: &Group{ID: groupID}})
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	upstream := &httpUpstreamRecorder{resp: newCodexHTTPSyntheticErrorResponse()}
	svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
	sessionHash := svc.GenerateSessionHash(c, body)
	svc.getOpenAIWSStateStore().MarkSessionInvalidEncryptedContent(
		groupID,
		sessionHash,
		[]string{openAIEncryptedContentDigest("stale-cipher")},
		time.Minute,
	)

	_, err := svc.Forward(context.Background(), c, codexBodyEncodingTestAccount(2015), body)
	require.Error(t, err)
	require.NotNil(t, upstream.lastReq)
	require.Contains(t, string(upstream.lastBody), "stale-cipher")
}

func TestCodexHTTPArbitrarySessionSeedUsesStableIsolatedV7(t *testing.T) {
	body := []byte(`{"model":"gpt-5.5","prompt_cache_key":"harness-session-123","input":[{"role":"user","content":"keep"}]}`)
	var accountA, accountB string
	for _, accountTest := range []struct {
		name     string
		account  *Account
		remember *string
	}{
		{name: "account a first", account: codexBodyEncodingTestAccount(2009), remember: &accountA},
		{name: "account a repeat", account: codexBodyEncodingTestAccount(2009), remember: &accountA},
		{name: "account b", account: codexBodyEncodingTestAccount(2010), remember: &accountB},
	} {
		t.Run(accountTest.name, func(t *testing.T) {
			if accountTest.name == "account b" {
				accountTest.account.Credentials["chatgpt_account_id"] = "other-chatgpt-account"
			}
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			upstream := captureCodexHTTPForward(
				t, c, accountTest.account, body, &config.Config{},
			)
			session := upstream.lastReq.Header.Get("session-id")
			parsed, err := uuid.Parse(session)
			require.NoError(t, err)
			require.Equal(t, uuid.Version(7), parsed.Version())
			require.Equal(t, session, gjson.GetBytes(upstream.lastBody, "prompt_cache_key").String())
			require.Equal(t, session, upstream.lastReq.Header.Get("thread-id"))
			require.Equal(t, session+":0", upstream.lastReq.Header.Get("x-codex-window-id"))
			if *accountTest.remember == "" {
				*accountTest.remember = session
			} else {
				require.Equal(t, *accountTest.remember, session)
			}
		})
	}
	require.NotEqual(t, accountA, accountB)
}

func TestCodexHTTPSyntheticIdentityUsesPinnedTimestamp(t *testing.T) {
	epoch, ok := uuidV7TimestampMillis(codexHTTPIdentityEpoch)
	require.True(t, ok)
	first := normalizeCodexHTTPIdentityID("arbitrary-session")
	second := normalizeCodexHTTPIdentityID("arbitrary-session")
	require.Equal(t, first, second)
	timestamp, ok := uuidV7TimestampMillis(first)
	require.True(t, ok)
	require.Equal(t, epoch, timestamp)
}

func TestCodexHTTPRejectsUnsupportedNonGenerationFieldInsteadOfDeletingIt(t *testing.T) {
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	body := []byte(`{"model":"gpt-5.5","input":[{"role":"user","content":"hi"}],"stream_options":{"include_usage":true}}`)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	upstream := &httpUpstreamRecorder{resp: newCodexHTTPSyntheticErrorResponse()}
	svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}

	_, err := svc.Forward(context.Background(), c, codexBodyEncodingTestAccount(2007), body)
	require.Error(t, err)
	require.Nil(t, upstream.lastReq)
	require.Equal(t, http.StatusBadRequest, recorder.Code)
	require.Equal(t, "request.stream_options", gjson.GetBytes(recorder.Body.Bytes(), "error.param").String())
}

func TestCodexHTTPResponseRestoresCallerToolNameAndStreamMode(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, passthrough := range []bool{false, true} {
		for _, stream := range []bool{false, true} {
			t.Run("passthrough="+boolString(passthrough)+"/stream="+boolString(stream), func(t *testing.T) {
				recorder := httptest.NewRecorder()
				c, _ := gin.CreateTestContext(recorder)
				body := []byte(`{"model":"gpt-5.5","stream":` + strconv.FormatBool(stream) + `,"input":[{"role":"user","content":"run python"}],"tools":[{"type":"function","name":"python","description":"Run Python","parameters":{"type":"object"}}]}`)
				c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
				SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
				wire := "data: {\"type\":\"response.output_item.added\",\"output_index\":0,\"item\":{\"type\":\"function_call\",\"id\":\"fc_test\",\"call_id\":\"call_test\",\"name\":\"" + codexPythonToolAlias + "\",\"arguments\":\"{}\"}}\n\n" +
					"data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_test\",\"status\":\"completed\",\"output\":[{\"type\":\"function_call\",\"id\":\"fc_test\",\"call_id\":\"call_test\",\"name\":\"" + codexPythonToolAlias + "\",\"arguments\":\"{}\"}],\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}}\n\n"
				upstream := &httpUpstreamRecorder{resp: &http.Response{
					StatusCode: http.StatusOK,
					Header:     http.Header{"Content-Type": []string{"text/event-stream"}},
					Body:       io.NopCloser(strings.NewReader(wire)),
				}}
				if passthrough && !stream {
					upstream.resp.Header.Set("Content-Type", "application/json")
				}
				account := codexBodyEncodingTestAccount(2008)
				account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
				setCodexHTTPTestMode(account, "gpt-5.5", false)
				svc := &OpenAIGatewayService{
					cfg:           &config.Config{},
					httpUpstream:  upstream,
					toolCorrector: NewCodexToolCorrector(),
				}

				result, err := svc.Forward(context.Background(), c, account, body)
				require.NoError(t, err)
				require.NotNil(t, result)
				require.Equal(t, stream, result.Stream)
				require.Contains(t, string(upstream.lastBody), codexPythonToolAlias)
				require.NotContains(t, string(upstream.lastBody), `"name":"python"`)
				if stream {
					require.Contains(t, recorder.Body.String(), `"name":"python"`)
					require.NotContains(t, recorder.Body.String(), codexPythonToolAlias)
				} else {
					require.Equal(t, "python", gjson.GetBytes(recorder.Body.Bytes(), "output.0.name").String())
					require.Contains(t, recorder.Header().Get("Content-Type"), "application/json")
				}
			})
		}
	}
}

func TestCodexHTTPIndexedRetryUsesSentLayout(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, passthrough := range []bool{false, true} {
		for _, tc := range []struct {
			name  string
			lite  bool
			tools bool
			index int
		}{
			{name: "standard", lite: false, tools: false, index: 3},
			{name: "lite_no_tools", lite: true, tools: false, index: 3},
			{name: "lite_with_tools", lite: true, tools: true, index: 4},
		} {
			t.Run("passthrough="+boolString(passthrough)+"/"+tc.name, func(t *testing.T) {
				toolsField := ""
				if tc.tools {
					toolsField = `"tools":[{"type":"function","name":"exec_command","description":"Run","parameters":{"type":"object"}}],`
				}
				body := []byte(`{"model":"gpt-5.5","instructions":"AGENT INSTRUCTIONS",` + toolsField + `"input":[{"type":"message","role":"user","status":"completed","content":"KEEP USER"},{"type":"custom_tool_call","name":"exec_command","id":"ctc_item","call_id":"call_a","status":"completed","input":"pwd"},{"type":"custom_tool_call_output","call_id":"call_a","output":"KEEP OUTPUT","status":"completed"}]}`)
				original := bytes.Clone(body)
				index := tc.index
				c, _ := gin.CreateTestContext(httptest.NewRecorder())
				c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
				SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
				upstream := &httpUpstreamRecorder{responses: []*http.Response{
					newOpenAIRejectedFieldTestResponse(
						http.StatusBadRequest,
						`{"error":{"code":"unknown_parameter","param":"input[`+strconv.Itoa(index)+`].status","message":"Unknown parameter"}}`,
					),
					newCodexHTTPSyntheticErrorResponse(),
				}}
				account := codexBodyEncodingTestAccount(2005)
				account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
				setCodexHTTPTestMode(account, "gpt-5.5", tc.lite)
				svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}

				_, err := svc.Forward(context.Background(), c, account, body)
				require.Error(t, err)
				require.Len(t, upstream.bodies, 2)
				require.Equal(t, "completed", gjson.GetBytes(upstream.bodies[0], `input.`+strconv.Itoa(index)+`.status`).String())
				require.False(t, gjson.GetBytes(upstream.bodies[1], `input.`+strconv.Itoa(index)+`.status`).Exists())
				require.Contains(t, string(upstream.bodies[1]), "KEEP OUTPUT")
				require.Equal(t,
					gjson.GetBytes(upstream.bodies[0], "prompt_cache_key").String(),
					gjson.GetBytes(upstream.bodies[1], "prompt_cache_key").String(),
				)
				require.Equal(t, original, body)
			})
		}
	}
}

func TestCodexHTTPDoesNotRetryByDroppingTaskContent(t *testing.T) {
	gin.SetMode(gin.TestMode)
	rejections := []string{
		`{"error":{"code":"invalid_encrypted_content","message":"invalid encrypted content"}}`,
		`{"error":{"code":"array_above_max_length","param":"input[1].content","message":"Invalid content: array too long. Maximum length 0"}}`,
		`{"error":{"code":"unknown_parameter","param":"tools","message":"Unsupported tools namespace"}}`,
		`{"error":{"code":"invalid_parameter","param":"prompt_cache_breakpoint","message":"prompt_cache_breakpoint is not supported on this model"}}`,
		`{"error":{"code":"invalid_parameter","param":"input[1].prompt_cache_breakpoint","message":"input[1].prompt_cache_breakpoint is not supported on this model"}}`,
	}
	for _, passthrough := range []bool{false, true} {
		for _, rejection := range rejections {
			name := firstNonEmpty(gjson.Get(rejection, "error.param").String(), gjson.Get(rejection, "error.code").String())
			t.Run("passthrough="+boolString(passthrough)+"/"+name, func(t *testing.T) {
				body := []byte(`{"model":"gpt-5.5","instructions":"keep task","prompt_cache_breakpoint":{"type":"message_start"},"input":[{"type":"reasoning","id":"rs_keep","summary":[],"encrypted_content":"opaque","content":[{"type":"reasoning_text","text":"KEEP REASONING"}],"prompt_cache_breakpoint":{"type":"message_end"}}],"tools":[{"type":"namespace","name":"functions","tools":[{"type":"function","name":"read_file","description":"Read","parameters":{"type":"object"}}]}]}`)
				c, _ := gin.CreateTestContext(httptest.NewRecorder())
				c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
				SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
				upstream := &httpUpstreamRecorder{responses: []*http.Response{
					newOpenAIRejectedFieldTestResponse(http.StatusBadRequest, rejection),
					newCodexHTTPSyntheticErrorResponse(),
				}}
				account := codexBodyEncodingTestAccount(2006)
				account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
				setCodexHTTPTestMode(account, "gpt-5.5", false)
				svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}

				_, err := svc.Forward(context.Background(), c, account, body)
				require.Error(t, err)
				require.Len(t, upstream.bodies, 1)
				require.False(t, gjson.GetBytes(upstream.bodies[0], "instructions").Exists())
				require.Equal(t, "developer", gjson.GetBytes(upstream.bodies[0], "input.0.role").String())
				require.Contains(t, string(upstream.bodies[0]), "KEEP REASONING")
				require.Contains(t, string(upstream.bodies[0]), "read_file")
				require.Equal(t, "message_start", gjson.GetBytes(upstream.bodies[0], "prompt_cache_breakpoint.type").String())
				require.Equal(t, "message_end", gjson.GetBytes(upstream.bodies[0], "input.1.prompt_cache_breakpoint.type").String())
			})
		}
	}
}

func captureCodexHTTPForward(
	t *testing.T,
	c *gin.Context,
	account *Account,
	body []byte,
	cfg *config.Config,
) *httpUpstreamRecorder {
	t.Helper()
	upstream := &httpUpstreamRecorder{resp: newCodexHTTPSyntheticErrorResponse()}
	svc := &OpenAIGatewayService{cfg: cfg, httpUpstream: upstream}
	_, err := svc.Forward(context.Background(), c, account, body)
	require.Error(t, err)
	require.NotNil(t, upstream.lastReq, "request must reach the synthetic upstream: %v", err)
	return upstream
}

func newCodexHTTPSyntheticErrorResponse() *http.Response {
	return &http.Response{
		StatusCode: http.StatusBadRequest,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body: io.NopCloser(strings.NewReader(
			`{"error":{"type":"invalid_request_error","message":"synthetic capture complete"}}`,
		)),
	}
}
