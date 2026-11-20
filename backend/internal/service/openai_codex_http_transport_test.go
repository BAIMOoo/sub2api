package service

import (
	"bytes"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
	"github.com/klauspost/compress/zstd"
	"github.com/stretchr/testify/require"
)

type codexHTTPTransportRecorder struct {
	httpUpstreamRecorder
	plainCalls int
	profiles   []*tlsfingerprint.Profile
}

func (u *codexHTTPTransportRecorder) Do(req *http.Request, proxy string, accountID int64, concurrency int) (*http.Response, error) {
	u.plainCalls++
	return u.httpUpstreamRecorder.Do(req, proxy, accountID, concurrency)
}

func (u *codexHTTPTransportRecorder) DoWithTLS(req *http.Request, proxy string, accountID int64, concurrency int, profile *tlsfingerprint.Profile) (*http.Response, error) {
	u.profiles = append(u.profiles, profile)
	return u.httpUpstreamRecorder.Do(req, proxy, accountID, concurrency)
}

func TestCodexHTTPTransportUsesFixedProfileAndStreamingZstd(t *testing.T) {
	for _, tc := range []struct {
		name, accountType, authMode, proxyScheme string
		passthrough                              bool
	}{
		{name: "OAuth", accountType: AccountTypeOAuth},
		{name: "PAT over SOCKS", accountType: AccountTypeOAuth, authMode: OpenAIAuthModePersonalAccessToken, proxyScheme: "socks5", passthrough: true},
		{name: "SetupToken over CONNECT", accountType: AccountTypeSetupToken, proxyScheme: "http"},
		{name: "OAuth over SOCKS5h", accountType: AccountTypeOAuth, proxyScheme: "socks5h"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			account := codexBodyEncodingTestAccount(331)
			account.Type = tc.accountType
			account.Credentials[openAIAuthModeCredentialKey] = tc.authMode
			account.Credentials["user_agent"] = "manual override"
			account.Extra = map[string]any{
				"openai_passthrough": tc.passthrough,
				"codex_cli_only":     true,
			}
			if tc.proxyScheme != "" {
				account.Proxy = &Proxy{ID: 1, Protocol: tc.proxyScheme, Host: "proxy.example.test", Port: 1080}
				account.ProxyID = &account.Proxy.ID
			}
			upstream := &codexHTTPTransportRecorder{}
			upstream.resp = &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{"Content-Type": {"text/event-stream"}},
				Body:       io.NopCloser(strings.NewReader(codexHTTPCompactSSECompleted)),
			}

			_, err := forwardFixedCodexTransportForTest(t, account, upstream, nil, "/v1/responses")
			require.NoError(t, err)
			require.Zero(t, upstream.plainCalls)
			require.Len(t, upstream.profiles, 1)
			require.Equal(t, tlsfingerprint.OpenAICodexLinuxProfile(), upstream.profiles[0])
			require.Equal(t, resolveAccountProxyURL(account), upstream.lastProxyURL)
			req := upstream.lastReq
			require.Equal(t, chatgptCodexURL, req.URL.String())
			require.Equal(t, codexExecHTTPUserAgent(), req.UserAgent())
			require.Equal(t, "text/event-stream", req.Header.Get("Accept"))
			require.Equal(t, "application/json", req.Header.Get("Content-Type"))
			require.Empty(t, req.Header.Get("Accept-Language"))
			require.Empty(t, req.Header.Get("OpenAI-Beta"))
			require.Equal(t, "zstd", req.Header.Get("Content-Encoding"))
			require.True(t, HTTPUpstreamRedirectsDisabled(req.Context()))
			require.Equal(t, HTTPUpstreamProfileCodexHTTP, HTTPUpstreamProfileFromContext(req.Context()))

			wire, err := io.ReadAll(req.Body)
			require.NoError(t, err)
			require.EqualValues(t, len(wire), req.ContentLength)
			var frame zstd.Header
			require.NoError(t, frame.Decode(wire))
			require.False(t, frame.SingleSegment || frame.HasFCS || frame.HasCheckSum)
			require.EqualValues(t, 2<<20, frame.WindowSize)
			require.Zero(t, frame.DictionaryID)
			preCompression, fixed := codexHTTPAttemptBody(req)
			require.True(t, fixed)
			require.Equal(t, preCompression, upstream.lastBody)
			replay, err := req.GetBody()
			require.NoError(t, err)
			t.Cleanup(func() { _ = replay.Close() })
			replayed, err := io.ReadAll(replay)
			require.NoError(t, err)
			require.Equal(t, wire, replayed)
		})
	}
}

func TestCodexHTTPTransportRefusesUnverifiedConfigBeforeDispatch(t *testing.T) {
	manager := &PluginManager{}
	manager.route.Store(&pluginRoute{pluginID: 1, rolloutPercent: 100, unavailable: "fixture"})
	for _, tc := range []struct {
		name, path, reason string
		proxy              *Proxy
		configuredProxy    bool
		configure          func(*Account)
		plugin             *PluginManager
	}{
		{name: "HTTPS proxy", proxy: &Proxy{Protocol: "https", Host: "proxy.example.test", Port: 443}, reason: "HTTPS proxy"},
		{name: "missing proxy", configuredProxy: true, reason: "configured proxy"},
		{name: "invalid URL", proxy: &Proxy{Protocol: "socks5", Host: "", Port: 1080}, reason: "invalid proxy"},
		{name: "invalid port", proxy: &Proxy{Protocol: "http", Host: "proxy.example.test", Port: 65536}, reason: "invalid proxy"},
		{name: "oversized headers", configure: func(a *Account) {
			a.Credentials["access_token"] = strings.Repeat("x", 64<<10)
		}, reason: "ordered transport limit"},
		{name: "invalid header value", configure: func(a *Account) {
			a.Credentials["chatgpt_account_id"] = "invalid\naccount"
		}, reason: "invalid HTTP header"},
		{name: "FedRAMP routing", configure: func(a *Account) {
			a.Credentials["chatgpt_account_is_fedramp"] = true
		}, reason: "FedRAMP"},
		{name: "TLS opt-out", configure: func(a *Account) {
			a.Extra["enable_tls_fingerprint"] = false
		}, reason: "TLS fingerprint opt-out"},
		{name: "custom TLS profile", configure: func(a *Account) {
			a.Extra["enable_tls_fingerprint"] = true
			a.Extra["tls_fingerprint_profile_id"] = int64(999)
		}, reason: "TLS fingerprint profile"},
		{name: "malformed TLS profile", configure: func(a *Account) {
			a.Extra["tls_fingerprint_profile_id"] = "999"
		}, reason: "TLS fingerprint profile"},
		{name: "plugin", plugin: manager, reason: "plugin"},
		{name: "unverified endpoint", path: "/v1/responses/unsupported", reason: "ChatGPT HTTPS endpoint"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			for _, passthrough := range []bool{false, true} {
				t.Run("passthrough="+boolString(passthrough), func(t *testing.T) {
					account := codexBodyEncodingTestAccount(332)
					account.Extra = map[string]any{"openai_passthrough": passthrough}
					if tc.configure != nil {
						tc.configure(account)
					}
					if tc.proxy != nil || tc.configuredProxy {
						proxyID := int64(1)
						account.ProxyID, account.Proxy = &proxyID, tc.proxy
					}
					upstream := &codexHTTPTransportRecorder{httpUpstreamRecorder: httpUpstreamRecorder{err: io.ErrUnexpectedEOF}}
					_, err := forwardFixedCodexTransportForTest(t, account, upstream, tc.plugin, tc.path)
					var configuration *CodexHTTPTransportConfigError
					require.ErrorAs(t, err, &configuration)
					require.ErrorContains(t, err, tc.reason)
					var failover *UpstreamFailoverError
					require.NotErrorAs(t, err, &failover)
					require.Empty(t, upstream.requests)
				})
			}
		})
	}
}

func TestCodexHTTPTransportProxyAuthIsNotFailover(t *testing.T) {
	for _, passthrough := range []bool{false, true} {
		t.Run("passthrough="+boolString(passthrough), func(t *testing.T) {
			account := codexBodyEncodingTestAccount(332)
			account.Extra = map[string]any{"openai_passthrough": passthrough}
			account.Proxy = &Proxy{ID: 1, Protocol: "http", Host: "proxy.example.test", Port: 1080}
			account.ProxyID = &account.Proxy.ID
			upstream := &codexHTTPTransportRecorder{httpUpstreamRecorder: httpUpstreamRecorder{err: tlsfingerprint.ErrProxyAuthentication}}

			_, err := forwardFixedCodexTransportForTest(t, account, upstream, nil, "/v1/responses")

			var configuration *CodexHTTPTransportConfigError
			require.ErrorAs(t, err, &configuration)
			require.ErrorContains(t, err, "proxy authentication rejected")
			var failover *UpstreamFailoverError
			require.NotErrorAs(t, err, &failover)
			require.Len(t, upstream.requests, 1)
		})
	}
}

func forwardFixedCodexTransportForTest(t *testing.T, account *Account, upstream *codexHTTPTransportRecorder, plugin *PluginManager, path string) (*OpenAIForwardResult, error) {
	t.Helper()
	if path == "" {
		path = "/v1/responses"
	}
	body := []byte(`{"model":"gpt-5.5","input":"a local fixture","stream":false}`)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, path, bytes.NewReader(body))
	c.Request.Header.Set("Accept", "application/json")
	c.Request.Header.Set("Content-Type", "application/json; charset=utf-8")
	c.Request.Header.Set("Accept-Language", "en")
	c.Request.Header.Set("OpenAI-Beta", "arbitrary-beta")
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream, pluginManager: plugin}
	return svc.Forward(c.Request.Context(), c, account, body)
}
