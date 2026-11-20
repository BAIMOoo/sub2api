package service

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestCodexHTTPPolicyUsesDirectIngressAndAccountProtocol(t *testing.T) {
	for _, test := range []struct {
		name      string
		platform  string
		account   string
		authMode  string
		transport OpenAIClientTransport
		aligned   bool
	}{
		{name: "oauth", platform: PlatformOpenAI, account: AccountTypeOAuth, transport: OpenAIClientTransportHTTP, aligned: true},
		{name: "pat", platform: PlatformOpenAI, account: AccountTypeOAuth, authMode: OpenAIAuthModePersonalAccessToken, transport: OpenAIClientTransportHTTP, aligned: true},
		{name: "setup token", platform: PlatformOpenAI, account: AccountTypeSetupToken, transport: OpenAIClientTransportHTTP, aligned: true},
		{name: "implicit oauth", account: AccountTypeOAuth, transport: OpenAIClientTransportHTTP, aligned: true},
		{name: "implicit pat", account: AccountTypeOAuth, authMode: OpenAIAuthModePersonalAccessToken, transport: OpenAIClientTransportHTTP, aligned: true},
		{name: "api key", platform: PlatformOpenAI, account: AccountTypeAPIKey, transport: OpenAIClientTransportHTTP},
		{name: "third party", platform: PlatformGrok, account: AccountTypeOAuth, transport: OpenAIClientTransportHTTP},
		{name: "unknown ingress", platform: PlatformOpenAI, account: AccountTypeOAuth},
		{name: "websocket", platform: PlatformOpenAI, account: AccountTypeOAuth, transport: OpenAIClientTransportWS},
	} {
		t.Run(test.name, func(t *testing.T) {
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
			SetOpenAIClientTransport(c, test.transport)

			account := codexBodyEncodingTestAccount(1001)
			account.Platform = test.platform
			account.Type = test.account
			account.Credentials[openAIAuthModeCredentialKey] = test.authMode

			require.Equal(t, test.aligned, codexHTTPPolicyForRequest(c, account).usesFixedExecContract())
		})
	}
}

func TestCodexHTTPFixedIdentityOverridesInboundAndConfiguredIdentity(t *testing.T) {
	gin.SetMode(gin.TestMode)
	pinNativeHTTPTestIdentity(t)

	for _, accountType := range []string{AccountTypeOAuth, AccountTypeSetupToken} {
		for _, passthrough := range []bool{false, true} {
			t.Run(accountType+"/passthrough="+boolString(passthrough), func(t *testing.T) {
				c, _ := gin.CreateTestContext(httptest.NewRecorder())
				c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
				c.Request.Header.Set("User-Agent", "arbitrary-harness/9")
				SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)

				account := codexBodyEncodingTestAccount(1002)
				account.Type = accountType
				account.Credentials["user_agent"] = "codex-tui/0.151.0 (Mac OS X; arm64)"

				svc := &OpenAIGatewayService{cfg: &config.Config{}}
				var (
					request *http.Request
					err     error
				)
				if passthrough {
					request, err = svc.buildUpstreamRequestOpenAIPassthrough(
						context.Background(), c, account, []byte(`{"model":"gpt-5.5","input":[]}`), "token",
					)
				} else {
					request, err = svc.buildUpstreamRequest(
						context.Background(), c, account, []byte(`{"model":"gpt-5.5","input":[]}`), "token", true, "", false,
					)
				}
				require.NoError(t, err)
				require.Equal(t, "codex_exec", request.Header.Get("originator"))
				require.Equal(t, codexExecHTTPVersion, request.Header.Get("version"))
				require.Equal(t, codexExecHTTPUserAgent(), request.Header.Get("User-Agent"))
			})
		}
	}
}

func TestCodexHTTPDirectIngressBypassesCodexCLIOnlyGate(t *testing.T) {
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	c.Request.Header.Set("User-Agent", "arbitrary-harness/1")
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)

	account := codexBodyEncodingTestAccount(1003)
	account.Extra = map[string]any{"codex_cli_only": true}
	result := (&OpenAIGatewayService{cfg: &config.Config{}}).detectCodexClientRestriction(
		c, account, []byte(`{"model":"gpt-5.5","input":[]}`),
	)

	require.True(t, result.Matched)
	require.Equal(t, "codex_http_contract", result.Reason)
}

func TestCodexHTTPModeSelectionUsesFinalCapabilityAndRecordsUnverifiedFallback(t *testing.T) {
	gin.SetMode(gin.TestMode)
	logs, restore := captureStructuredLog(t)
	defer restore()

	for _, test := range []struct {
		name       string
		capability string
		declared   bool
		wantLite   bool
		wantStatus string
	}{
		{name: "known standard", capability: "false", declared: true, wantStatus: "baseline_selected"},
		{name: "known lite", capability: "true", wantStatus: "baseline_selected", wantLite: true},
		{name: "unknown declared lite", declared: true, wantStatus: "unverified", wantLite: true},
		{name: "unknown standard default", wantStatus: "unverified"},
	} {
		t.Run(test.name, func(t *testing.T) {
			body := []byte(`{"model":"agent-model","instructions":"caller task","tools":[{"type":"function","name":"read_file","description":"Read","parameters":{"type":"object"}}],"input":[{"type":"message","role":"user","content":"hello"}]}`)
			originalBody := bytes.Clone(body)
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
			if test.declared {
				c.Request.Header.Set(responsesLiteHeader, "true")
			}
			originalHeaders := c.Request.Header.Clone()
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)

			account := codexBodyEncodingTestAccount(1004)
			if test.capability != "" {
				account.SetUpstreamModelMetadataSnapshot(UpstreamModelMetadataSnapshot{
					Models: map[string]UpstreamModelMetadata{
						"agent-model": {
							ID: "agent-model",
							CodexToolCapabilities: map[string]json.RawMessage{
								"use_responses_lite": json.RawMessage(test.capability),
							},
						},
					},
				})
			}

			normalized, mode, err := normalizeCodexHTTPBody(c, account, body)
			require.NoError(t, err)
			require.Equal(t, test.wantLite, mode.lite)
			require.Equal(t, test.wantStatus, string(mode.alignment))
			require.Equal(t, originalBody, body)
			require.Equal(t, originalHeaders, c.Request.Header)
			if test.wantLite {
				require.False(t, gjson.GetBytes(normalized, "instructions").Exists())
				require.Equal(t, "read_file", gjson.GetBytes(normalized, "input.0.tools.0.tools.0.name").String())
			} else {
				require.Equal(t, "read_file", gjson.GetBytes(normalized, "tools.0.name").String())
			}
		})
	}

	require.True(t, logs.ContainsFieldValue("alignment_status", "unverified"))
	require.False(t, logs.ContainsFieldValue("request_body", "caller task"))
}

func TestCodexHTTPModeBodyAdaptsCompactForStreamingResponses(t *testing.T) {
	body := []byte(`{"model":"gpt-5.5","instructions":"keep compact task","input":[{"role":"user","content":"compact me"}],"tools":[{"type":"function","name":"read_file","description":"Read","parameters":{"type":"object"}}]}`)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses/compact", bytes.NewReader(body))
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)

	normalized, _, err := normalizeCodexHTTPBody(c, codexBodyEncodingTestAccount(1005), body)
	require.NoError(t, err)
	require.True(t, gjson.GetBytes(normalized, "stream").Bool())
	require.Equal(t, "keep compact task", gjson.GetBytes(normalized, "instructions").String())
	require.Equal(t, "compaction_trigger", gjson.GetBytes(normalized, "input.1.type").String())
}

func TestCodexHTTPStandardRejectsUnrepresentableAdditionalToolsCarrier(t *testing.T) {
	body := []byte(`{"model":"gpt-5.5","input":[{"type":"additional_tools","role":"developer","tools":[],"content":"must not disappear"}]}`)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)

	_, _, err := normalizeCodexHTTPBody(c, codexBodyEncodingTestAccount(1006), body)
	require.Error(t, err)
	var validationErr *openAIResponsesLiteValidationError
	require.ErrorAs(t, err, &validationErr)
	require.Equal(t, "input", validationErr.param)
}

func boolString(value bool) string {
	if value {
		return "true"
	}
	return "false"
}

func setCodexHTTPTestMode(account *Account, model string, lite bool) {
	account.SetUpstreamModelMetadataSnapshot(UpstreamModelMetadataSnapshot{
		Models: map[string]UpstreamModelMetadata{
			model: {
				ID: model,
				CodexToolCapabilities: map[string]json.RawMessage{
					"use_responses_lite": json.RawMessage(boolString(lite)),
				},
			},
		},
	})
}
