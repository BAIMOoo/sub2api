package service

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestCodexHTTPEgress_IndexedRetryUsesSentLayout(t *testing.T) {
	for _, passthrough := range []bool{false, true} {
		t.Run(strconv.FormatBool(passthrough), func(t *testing.T) {
			body := []byte(`{"model":"gpt-6-astra","instructions":"AGENT INSTRUCTIONS","input":[{"type":"message","role":"user","status":"completed","content":"KEEP USER"},{"type":"custom_tool_call","name":"exec_command","id":"ctc_item","call_id":"call_a","status":"completed","input":"pwd"},{"type":"custom_tool_call_output","call_id":"call_a","output":"KEEP OUTPUT"}]}`)
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			upstream := &httpUpstreamRecorder{responses: []*http.Response{
				newOpenAIRejectedFieldTestResponse(400, `{"error":{"code":"unknown_parameter","param":"input[2].status","message":"Unknown parameter: input[2].status"}}`),
				newOpenAIRejectedFieldTestResponse(400, `{"error":{"message":"synthetic stop"}}`),
			}}
			account := codexBodyEncodingTestAccount(1600)
			account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
			svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
			_, err := svc.Forward(context.Background(), c, account, body)
			require.Error(t, err)
			require.Len(t, upstream.bodies, 2)
			require.Equal(t, "AGENT INSTRUCTIONS", gjson.GetBytes(upstream.bodies[0], "input.1.content.0.text").String())
			require.Equal(t, "completed", gjson.GetBytes(upstream.bodies[0], "input.2.status").String())
			require.False(t, gjson.GetBytes(upstream.bodies[1], "input.2.status").Exists())
			require.Equal(t, "completed", gjson.GetBytes(upstream.bodies[1], "input.3.status").String())
			require.Equal(t, "KEEP OUTPUT", gjson.GetBytes(upstream.bodies[1], "input.4.output").String())
			require.Equal(t, "completed", gjson.GetBytes(body, "input.0.status").String())
		})
	}
}

func TestCodexHTTPEgress_CompactUsesOfficialV2Wire(t *testing.T) {
	for _, passthrough := range []bool{false, true} {
		t.Run(strconv.FormatBool(passthrough), func(t *testing.T) {
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			body := []byte(`{"model":"gpt-5.5","instructions":"keep agent rules","input":[{"role":"user","content":"keep conversation"}]}`)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses/compact", bytes.NewReader(body))
			c.Request.Header.Set("x-codex-beta-features", "code_mode")
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			upstream := &httpUpstreamRecorder{resp: &http.Response{
				StatusCode: 200, Header: http.Header{"Content-Type": []string{"text/event-stream"}},
				Body: io.NopCloser(strings.NewReader("data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_compact\",\"object\":\"response\",\"status\":\"completed\",\"output\":[{\"type\":\"compaction\",\"id\":\"cmp_keep\",\"encrypted_content\":\"opaque-keep\"}],\"usage\":{\"input_tokens\":10,\"output_tokens\":2}}}\n\n")),
			}}
			account := codexBodyEncodingTestAccount(1800)
			account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
			svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
			_, err := svc.Forward(context.Background(), c, account, body)
			require.NoError(t, err)
			require.Equal(t, chatgptCodexURL, upstream.lastReq.URL.String())
			require.Equal(t, "text/event-stream", upstream.lastReq.Header.Get("Accept"))
			require.Contains(t, upstream.lastReq.Header.Get("x-codex-beta-features"), "remote_compaction_v2")
			require.True(t, gjson.GetBytes(upstream.lastBody, "stream").Bool())
			require.Equal(t, "keep agent rules", gjson.GetBytes(upstream.lastBody, "instructions").String())
			require.Equal(t, "keep conversation", gjson.GetBytes(upstream.lastBody, "input.0.content").String())
			require.Equal(t, "compaction_trigger", gjson.GetBytes(upstream.lastBody, "input.1.type").String())
			require.Equal(t, "compaction", gjson.Parse(upstream.lastReq.Header.Get("x-codex-turn-metadata")).Get("request_kind").String())
			require.Equal(t, "response.compaction", gjson.GetBytes(recorder.Body.Bytes(), "object").String())
			require.Equal(t, "opaque-keep", gjson.GetBytes(recorder.Body.Bytes(), "output.0.encrypted_content").String())
			require.Equal(t, "cmp_keep", gjson.GetBytes(recorder.Body.Bytes(), "output.0.id").String())
		})
	}
}

func TestCodexHTTPEgress_CompactRejectsIncompleteStream(t *testing.T) {
	for _, passthrough := range []bool{false, true} {
		recorder := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(recorder)
		body := []byte(`{"model":"gpt-5.5","input":[{"role":"user","content":"summarize"}]}`)
		c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses/compact", bytes.NewReader(body))
		SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
		upstream := &httpUpstreamRecorder{resp: &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: io.NopCloser(strings.NewReader("data: {\"type\":\"response.created\",\"response\":{\"id\":\"resp_incomplete\"}}\n\n"))}}
		account := codexBodyEncodingTestAccount(2102)
		account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
		svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
		_, err := svc.Forward(context.Background(), c, account, body)
		require.Error(t, err)
		require.Equal(t, 502, recorder.Code)
		require.NotContains(t, recorder.Body.String(), "data: ")
	}
}

func TestCodexHTTPEgress_ResponseKeepsCallerToolNameAndStreamMode(t *testing.T) {
	for _, passthrough := range []bool{false, true} {
		for _, stream := range []bool{false, true} {
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			body := []byte(`{"model":"gpt-5.5","stream":` + strconv.FormatBool(stream) + `,"input":[{"role":"user","content":"read"}],"tools":[{"type":"function","name":"read_file","description":"Read","parameters":{"type":"object"}}]}`)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			wire := "data: {\"type\":\"response.output_item.added\",\"output_index\":0,\"item\":{\"type\":\"function_call\",\"id\":\"fc_test\",\"call_id\":\"call_test\",\"name\":\"read_file\",\"arguments\":\"{}\"}}\n\n" +
				"data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_test\",\"status\":\"completed\",\"output\":[{\"type\":\"function_call\",\"id\":\"fc_test\",\"call_id\":\"call_test\",\"name\":\"read_file\",\"arguments\":\"{}\"}],\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}}\n\n"
			upstream := &httpUpstreamRecorder{resp: &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: io.NopCloser(strings.NewReader(wire))}}
			if passthrough && !stream {
				// Reproduced against the live Codex backend: body is SSE while
				// headers advertise JSON. Caller still requested a unary response.
				upstream.resp.Header.Set("Content-Type", "application/json")
			}
			account := codexBodyEncodingTestAccount(2200)
			account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
			svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream, toolCorrector: NewCodexToolCorrector()}
			result, err := svc.Forward(context.Background(), c, account, body)
			require.NoError(t, err)
			require.Equal(t, stream, result.Stream)
			if stream {
				require.Contains(t, recorder.Body.String(), `"name":"read_file"`)
			} else {
				require.Equal(t, "read_file", gjson.GetBytes(recorder.Body.Bytes(), "output.0.name").String())
				require.Contains(t, recorder.Header().Get("Content-Type"), "application/json")
			}
		}
	}
}

func TestCodexHTTPEgress_DoesNotRetryByDroppingReasoning(t *testing.T) {
	for _, passthrough := range []bool{false, true} {
		for _, rejection := range []string{
			`{"error":{"code":"invalid_encrypted_content","message":"invalid encrypted content"}}`,
			`{"error":{"code":"array_above_max_length","param":"input[0].content","message":"Invalid 'input[0].content': array too long. Maximum length 0"}}`,
		} {
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			body := []byte(`{"model":"gpt-5.5","instructions":"keep","input":[{"type":"reasoning","id":"rs_keep","summary":[],"encrypted_content":"gAAAA_keep","content":[{"type":"reasoning_text","text":"KEEP REASONING"}]}]}`)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			upstream := &httpUpstreamRecorder{responses: []*http.Response{
				newOpenAIRejectedFieldTestResponse(400, rejection),
				newOpenAIRejectedFieldTestResponse(400, `{"error":{"message":"unexpected retry"}}`),
			}}
			account := codexBodyEncodingTestAccount(1601)
			account.Extra = map[string]any{"openai_oauth_passthrough": passthrough}
			svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
			_, err := svc.Forward(context.Background(), c, account, body)
			require.Error(t, err)
			require.Len(t, upstream.bodies, 1)
			require.Contains(t, string(upstream.bodies[0]), "KEEP REASONING")
		}
	}
}

type codexHTTPTransportRecorder struct {
	plainCalls int
	profile    *tlsfingerprint.Profile
}

func (u *codexHTTPTransportRecorder) Do(*http.Request, string, int64, int) (*http.Response, error) {
	u.plainCalls++
	return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader("legacy"))}, nil
}

func (u *codexHTTPTransportRecorder) DoWithTLS(_ *http.Request, _ string, _ int64, _ int, profile *tlsfingerprint.Profile) (*http.Response, error) {
	u.profile = profile
	return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader("fixed"))}, nil
}

func TestCodexHTTPEgress_EnforcesTransportWithoutChangingWS(t *testing.T) {
	for _, accountType := range []string{AccountTypeOAuth, AccountTypeSetupToken} {
		for _, transport := range []OpenAIClientTransport{OpenAIClientTransportHTTP, OpenAIClientTransportWS} {
			for _, proxyURL := range []string{"", "http://localhost:8080", "socks5://localhost:1080", "https://localhost:8080"} {
				c, _ := gin.CreateTestContext(httptest.NewRecorder())
				c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
				SetOpenAIClientTransport(c, transport)
				account := codexBodyEncodingTestAccount(1700)
				account.Type = accountType
				account.Extra = map[string]any{"enable_tls_fingerprint": false, "tls_fingerprint_profile_id": 999}
				upstream := &codexHTTPTransportRecorder{}
				svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
				req, err := svc.buildUpstreamRequest(context.Background(), c, account, []byte(`{"model":"gpt-5.5","input":[]}`), "fake", true, "", false)
				require.NoError(t, err)
				resp, err := svc.doOpenAIUpstream(req, proxyURL, account)
				if transport == OpenAIClientTransportHTTP && strings.HasPrefix(proxyURL, "https:") {
					var configErr *codexHTTPTransportConfigError
					require.ErrorAs(t, err, &configErr)
					require.Nil(t, upstream.profile)
					require.Zero(t, upstream.plainCalls)
					continue
				}
				require.NoError(t, err)
				require.NoError(t, resp.Body.Close())
				if transport == OpenAIClientTransportHTTP {
					require.Equal(t, tlsfingerprint.OpenAICodexLinuxProfile().Identity(), upstream.profile.Identity())
					require.Zero(t, upstream.plainCalls)
				} else {
					require.Nil(t, upstream.profile)
					require.Equal(t, 1, upstream.plainCalls)
				}
			}
		}
	}
}

func TestCodexHTTPEgress_RejectsUnverifiedPlugin(t *testing.T) {
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	manager := &PluginManager{}
	manager.route.Store(&pluginRoute{pluginID: 1, rolloutPercent: 100, unavailable: "synthetic"})
	upstream := &codexHTTPTransportRecorder{}
	svc := &OpenAIGatewayService{cfg: &config.Config{}, pluginManager: manager, httpUpstream: upstream}
	account := codexBodyEncodingTestAccount(1701)
	req, err := svc.buildUpstreamRequest(context.Background(), c, account, []byte(`{"model":"gpt-5.5","input":[]}`), "fake", true, "", false)
	require.NoError(t, err)
	_, err = svc.doOpenAIUpstream(req, "", account)
	var configErr *codexHTTPTransportConfigError
	require.ErrorAs(t, err, &configErr)
	require.Zero(t, upstream.plainCalls)
	require.Nil(t, upstream.profile)
	err = svc.handleOpenAIUpstreamTransportError(context.Background(), c, account, err, false)
	require.ErrorAs(t, err, &configErr)
	require.Equal(t, http.StatusBadGateway, c.Writer.Status())
}
