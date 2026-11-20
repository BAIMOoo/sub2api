package repository

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// Exercise both real ingress paths against the same account, gateway and cookie jar.
// Only the store and the final upstream connection are replaced; no provider is contacted.
func TestOpenAIWSHTTPBridgeAndHTTPUseSeparateTransports(t *testing.T) {
	for _, tc := range []struct {
		name       string
		accountTLS bool
	}{{"default", false}, {"account_TLS_enabled", true}} {
		t.Run(tc.name, func(t *testing.T) {
			store := &fakeUpstreamCookieStore{jar: []service.UpstreamCookie{{Name: "_cfuvid", Value: "test-http-only"}}}
			var calls int
			origin := newRecordingUpstream(func(*http.Request) *http.Response {
				calls++
				if calls == 2 { // The second WS turn must not clear the HTTP jar on a 403.
					return emptyResponse(http.StatusForbidden, nil)
				}
				const completed = `data: {"type":"response.completed","response":{"id":"resp_test","status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}`
				return &http.Response{
					StatusCode: http.StatusOK,
					Header:     http.Header{"Content-Type": {"text/event-stream"}, "Set-Cookie": {"__cf_bm=test-response; Path=/"}},
					Body:       io.NopCloser(strings.NewReader(completed + "\n\n")),
				}
			})
			cfg := &config.Config{}
			cfg.Gateway.OpenAIWS = config.GatewayOpenAIWSConfig{
				Enabled: true, OAuthEnabled: true, ResponsesWebsocketsV2: true, ModeRouterV2Enabled: true,
				IngressModeDefault: service.OpenAIWSIngressModeHTTPBridge,
			}
			gateway := service.ProvideOpenAIGatewayService(nil, nil, nil, nil, nil, nil, nil, cfg, nil, nil, nil, nil, nil,
				NewUpstreamCookieJar(origin, store), nil, nil, nil, nil, nil, nil, nil, nil, &service.TLSFingerprintProfileService{})
			t.Cleanup(gateway.CloseOpenAIWSPool)
			account := &service.Account{
				ID: 1, Platform: service.PlatformOpenAI, Type: service.AccountTypeOAuth, Concurrency: 1,
				Credentials: map[string]any{"access_token": "test-token"},
				Extra: map[string]any{
					"openai_oauth_responses_websockets_v2_mode": service.OpenAIWSIngressModeHTTPBridge,
				},
			}
			if tc.accountTLS {
				account.Extra["enable_tls_fingerprint"] = true
			}

			runCookieJarWSBridge(t, gateway, account)
			require.Len(t, origin.requests, 2)
			for i, req := range origin.requests {
				require.Equal(t, codexResponsesURL, req.URL.String())
				require.True(t, service.IsOpenAIWSHTTPBridgeUpstream(req.Context()))
				require.Equal(t, service.HTTPUpstreamProfileOpenAI, service.HTTPUpstreamProfileFromContext(req.Context()))
				require.Empty(t, req.Header.Get("Content-Encoding"))
				require.Empty(t, origin.cookieAt(i), "bridge must not attach the HTTP account cookie")
			}
			require.Empty(t, origin.tlsProfiles, "bridge must call the ordinary Do method")
			require.Zero(t, store.loads, "bridge must not even read the HTTP jar")
			require.Zero(t, store.saves, "bridge response must not update the HTTP jar")
			require.Zero(t, store.clears, "bridge 403 must not clear the HTTP jar")

			account.Extra["openai_oauth_responses_websockets_v2_mode"] = service.OpenAIWSIngressModeCtxPool
			require.Equal(t, service.OpenAIUpstreamTransportResponsesWebsocketV2,
				service.NewOpenAIWSProtocolResolver(cfg).Resolve(account).Transport)
			const body = `{"model":"gpt-5","instructions":"Test","input":"hello","stream":true}`
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequestWithContext(t.Context(), http.MethodPost, "/v1/responses", strings.NewReader(body))
			service.SetOpenAIClientTransport(c, service.OpenAIClientTransportHTTP)
			result, err := gateway.Forward(t.Context(), c, account, []byte(body))
			require.NoError(t, err)
			require.False(t, result.OpenAIWSMode, "the WS mode setting must not select WS egress for direct HTTP")
			require.Len(t, origin.requests, 3)
			direct := origin.requests[2]
			require.Equal(t, codexResponsesURL, direct.URL.String())
			require.False(t, service.IsOpenAIWSHTTPBridgeUpstream(direct.Context()))
			require.Equal(t, service.HTTPUpstreamProfileCodexHTTP, service.HTTPUpstreamProfileFromContext(direct.Context()))
			require.Equal(t, "codex_exec", direct.Header.Get("Originator"))
			require.Equal(t, "0.158.0", direct.Header.Get("Version"))
			require.Equal(t, "zstd", direct.Header.Get("Content-Encoding"))
			require.Equal(t, "_cfuvid=test-http-only", origin.cookieAt(2))
			require.Positive(t, store.saves, "ordinary HTTP still harvests Set-Cookie")
			require.Equal(t, []*tlsfingerprint.Profile{tlsfingerprint.OpenAICodexLinuxProfile()}, origin.tlsProfiles)
		})
	}
}

func runCookieJarWSBridge(t *testing.T, gateway *service.OpenAIGatewayService, account *service.Account) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	done := make(chan error, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, nil)
		if err != nil {
			done <- err
			return
		}
		defer func() { _ = conn.CloseNow() }()
		_, first, err := conn.Read(ctx)
		if err != nil {
			done <- err
			return
		}
		c, _ := gin.CreateTestContext(w)
		c.Request = r
		service.SetOpenAIClientTransport(c, service.OpenAIClientTransportWS)
		done <- gateway.ProxyResponsesWebSocketFromClient(ctx, c, conn, account, "test-token", first, nil)
	}))
	defer server.Close()
	client, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(server.URL, "http")+"/v1/responses", nil)
	require.NoError(t, err)
	defer func() { _ = client.CloseNow() }()
	for _, want := range []string{"response.completed", "error"} {
		require.NoError(t, client.Write(ctx, websocket.MessageText, []byte(`{"type":"response.create","model":"gpt-5","input":"hello"}`)))
		_, event, err := client.Read(ctx)
		require.NoError(t, err)
		require.Equal(t, want, gjson.GetBytes(event, "type").String())
		if want == "error" {
			require.EqualValues(t, http.StatusForbidden, gjson.GetBytes(event, "status").Int())
		}
	}
	select {
	case err := <-done:
		require.Error(t, err, "expected the second turn to fail")
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
}
