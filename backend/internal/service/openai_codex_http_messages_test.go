package service

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/claude"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestCodexHTTPMessagesPreservesCallerSemanticsWithoutInjection(t *testing.T) {
	gin.SetMode(gin.TestMode)
	pinNativeHTTPTestIdentity(t)
	logs, restore := captureStructuredLog(t)
	defer restore()

	for _, credential := range []struct {
		name string
		kind string
		mode string
	}{
		{name: "oauth", kind: AccountTypeOAuth},
		{name: "pat", kind: AccountTypeOAuth, mode: OpenAIAuthModePersonalAccessToken},
		{name: "setup token", kind: AccountTypeSetupToken},
	} {
		for _, lite := range []bool{false, true} {
			t.Run(credential.name+"/lite="+boolString(lite), func(t *testing.T) {
				body := []byte(`{
					"model":"gpt-5.5",
					"max_tokens":16,
					"temperature":0.4,
					"top_k":40,
					"thinking":{"type":"enabled","budget_tokens":64},
					"metadata":{"caller":"KEEP MESSAGES METADATA"},
					"system":[{"type":"text","text":"KEEP SYSTEM BLOCK"}],
					"messages":[
						{"role":"user","content":"KEEP USER TASK"},
						{"role":"assistant","content":[
							{"type":"thinking","thinking":"KEEP SUMMARY","signature":"gAAAA-reasoning"},
							{"type":"tool_use","id":"toolu_messages","name":"lookup","input":{"id":9007199254740993}}
						]},
						{"role":"user","content":[
							{"type":"tool_result","tool_use_id":"toolu_messages","content":[
								{"type":"text","text":"KEEP TOOL RESULT"},
								{"type":"image","source":{"type":"base64","media_type":"image/png","data":"AQID"}}
							]}
						]}
					],
					"tools":[{
						"name":"lookup",
						"description":"Lookup",
						"input_schema":{"type":"object","properties":{"id":{"type":"integer","const":9007199254740993}}}
					}],
					"tool_choice":{"type":"tool","name":"lookup","disable_parallel_tool_use":true},
					"stream":false
				}`)
				originalBody := bytes.Clone(body)
				account := codexBodyEncodingTestAccount(4200)
				account.Type = credential.kind
				account.Credentials[openAIAuthModeCredentialKey] = credential.mode
				setCodexHTTPTestMode(account, "gpt-5.5", lite)

				recorder := httptest.NewRecorder()
				c, _ := gin.CreateTestContext(recorder)
				c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", bytes.NewReader(body))
				c.Request.Header.Set("User-Agent", "arbitrary-messages-harness/1")
				c.Request.Header.Set("anthropic-beta", claude.BetaFastMode)
				originalHeaders := c.Request.Header.Clone()
				SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
				upstream := &httpUpstreamRecorder{resp: newCodexHTTPSyntheticErrorResponse()}
				svc := &OpenAIGatewayService{
					cfg: &config.Config{Gateway: config.GatewayConfig{
						ForcedCodexInstructionsTemplate: "SERVER TASK MUST NOT APPEAR",
					}},
					httpUpstream: upstream,
				}

				_, err := svc.ForwardAsAnthropic(context.Background(), c, account, body, "caller-cache-key", "")
				require.Error(t, err)
				require.NotNil(t, upstream.lastReq)
				require.Equal(t, originalBody, body)
				require.Equal(t, originalHeaders, c.Request.Header)
				require.Equal(t, codexExecHTTPVersion, upstream.lastReq.Header.Get("version"))
				require.Equal(t, codexExecHTTPUserAgent(), upstream.lastReq.Header.Get("User-Agent"))
				require.Empty(t, upstream.lastReq.Header.Get("OpenAI-Beta"))
				session := gjson.GetBytes(upstream.lastBody, "prompt_cache_key").String()
				require.Equal(t, normalizeCodexHTTPIdentityID(scopeCodexAccountIdentityValue(account, 0, "prompt-cache", "caller-cache-key")), session)
				for _, header := range []string{"session-id", "thread-id", "x-client-request-id"} {
					require.Equal(t, session, upstream.lastReq.Header.Get(header))
				}
				require.Contains(t, string(upstream.lastBody), "KEEP SYSTEM BLOCK")
				require.Contains(t, string(upstream.lastBody), "KEEP USER TASK")
				require.Contains(t, string(upstream.lastBody), "KEEP SUMMARY")
				require.Contains(t, string(upstream.lastBody), "KEEP TOOL RESULT")
				require.Contains(t, string(upstream.lastBody), "KEEP MESSAGES METADATA")
				require.Contains(t, string(upstream.lastBody), "9007199254740993")
				require.Equal(t, "toolu_messages", gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call").call_id`).String())
				require.Equal(t, "toolu_messages", gjson.GetBytes(upstream.lastBody, `input.#(type=="function_call_output").call_id`).String())
				require.NotContains(t, string(upstream.lastBody), "SERVER TASK MUST NOT APPEAR")
				require.NotContains(t, string(upstream.lastBody), "sub2api-claude-code-todo-guard")
				require.False(t, gjson.GetBytes(upstream.lastBody, "max_tokens").Exists())
				require.False(t, gjson.GetBytes(upstream.lastBody, "temperature").Exists())
				require.False(t, gjson.GetBytes(upstream.lastBody, "top_k").Exists())
				require.Equal(t, "priority", gjson.GetBytes(upstream.lastBody, "service_tier").String())
				require.Equal(t, lite, isOpenAIResponsesLiteHeader(upstream.lastReq.Header.Get(responsesLiteHeader)))
				if lite {
					require.False(t, gjson.GetBytes(upstream.lastBody, "tools").Exists())
					require.True(t, gjson.GetBytes(upstream.lastBody, `input.#(type=="additional_tools")`).Exists())
				} else {
					require.Equal(t, "lookup", gjson.GetBytes(upstream.lastBody, "tools.0.name").String())
				}
			})
		}
	}
	for _, field := range []string{"max_tokens", "temperature", "top_k", "thinking.budget_tokens"} {
		require.True(t, logs.ContainsFieldValue("ignored_parameters", field), field)
	}
}

func TestCodexHTTPMessagesRejectsUnrepresentableContentBeforeUpstream(t *testing.T) {
	for _, test := range []struct {
		name string
		body string
		lite bool
	}{
		{
			name: "unknown top level",
			body: `{"model":"gpt-5.5","messages":[{"role":"user","content":"hi"}],"context_management":{"edits":[]}}`,
		},
		{
			name: "unknown block",
			body: `{"model":"gpt-5.5","messages":[{"role":"user","content":[{"type":"document","source":{"type":"text","data":"lost"}}]}]}`,
		},
		{
			name: "orphan result",
			body: `{"model":"gpt-5.5","messages":[{"role":"user","content":[{"type":"tool_result","tool_use_id":"missing","content":"lost"}]}]}`,
		},
		{
			name: "invalid image",
			body: `{"model":"gpt-5.5","messages":[{"role":"user","content":[{"type":"image","source":{"type":"base64","media_type":"image/png","data":""}}]}]}`,
		},
		{
			name: "explicit parallel tools in Lite",
			body: `{"model":"gpt-5.5","messages":[{"role":"user","content":"hi"}],"tools":[{"name":"lookup","input_schema":{"type":"object"}}],"tool_choice":{"type":"auto","disable_parallel_tool_use":false}}`,
			lite: true,
		},
		{
			name: "implicit parallel tools in Lite",
			body: `{"model":"gpt-5.5","messages":[{"role":"user","content":"hi"}],"tools":[{"name":"lookup","input_schema":{"type":"object"}}],"tool_choice":{"type":"auto"}}`,
			lite: true,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", bytes.NewReader([]byte(test.body)))
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			upstream := &httpUpstreamRecorder{resp: newCodexHTTPSyntheticErrorResponse()}
			svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
			account := codexBodyEncodingTestAccount(4201)
			setCodexHTTPTestMode(account, "gpt-5.5", test.lite)

			_, err := svc.ForwardAsAnthropic(
				context.Background(),
				c,
				account,
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

func TestCodexHTTPMessagesLeavesWebSocketAndAPIKeyCompatibilityPathsUnchanged(t *testing.T) {
	body := []byte(`{"model":"gpt-5.5","max_tokens":16,"system":"client system","messages":[{"role":"user","content":"client task"}],"stream":false}`)

	for _, test := range []struct {
		name      string
		transport OpenAIClientTransport
		account   *Account
	}{
		{
			name:      "websocket",
			transport: OpenAIClientTransportWS,
			account:   codexBodyEncodingTestAccount(4202),
		},
		{
			name:      "api key",
			transport: OpenAIClientTransportHTTP,
			account: &Account{
				ID:          4203,
				Platform:    PlatformOpenAI,
				Type:        AccountTypeAPIKey,
				Credentials: map[string]any{"api_key": "sk-test"},
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", bytes.NewReader(body))
			SetOpenAIClientTransport(c, test.transport)
			upstream := &httpUpstreamRecorder{resp: newCodexHTTPSyntheticErrorResponse()}
			svc := &OpenAIGatewayService{
				cfg: &config.Config{Gateway: config.GatewayConfig{
					ForcedCodexInstructionsTemplate: "LEGACY SERVER PREFIX\n{{ .ExistingInstructions }}",
				}},
				httpUpstream: upstream,
			}

			_, err := svc.ForwardAsAnthropic(context.Background(), c, test.account, body, "stable-cache", "")
			require.Error(t, err)
			require.NotNil(t, upstream.lastReq)
			require.Contains(t, string(upstream.lastBody), "sub2api-claude-code-todo-guard")
			if test.account.UsesOpenAICodexProtocol() {
				require.Contains(t, string(upstream.lastBody), "LEGACY SERVER PREFIX")
				require.NotEqual(t, codexExecHTTPVersion, upstream.lastReq.Header.Get("version"))
			}
		})
	}
}
