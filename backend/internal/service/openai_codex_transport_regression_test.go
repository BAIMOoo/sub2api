package service

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestCodexWireDefaultsDoNotLeakThroughSettingsCache(t *testing.T) {
	const version = "0.204.0"
	const wantWS = "codex_cli_rs/0.204.0 (Ubuntu 22.4.0; x86_64) xterm-256color"
	wantHTTP := buildCodexCLIUserAgent(version)
	ctx := context.Background()
	for _, tt := range []struct{ name, panelUA, wantWS string }{
		{"empty", "", wantWS},
		// An explicitly saved string is not the same as an empty panel field.
		{"explicit HTTP default", codexCLIUserAgent, wantHTTP},
	} {
		t.Run(tt.name, func(t *testing.T) {
			s := NewSettingService(&codexVersionSettingRepoStub{values: map[string]string{
				SettingKeyOpenAICodexUserAgent: tt.panelUA, SettingKeyOpenAICodexClientVersionSynced: version,
			}}, nil)
			require.Equal(t, tt.wantWS, s.codexCanonicalUAFor(ctx, upstreamWSWire))
			require.Equal(t, wantHTTP, s.GetOpenAICodexCanonicalUserAgent(ctx))
			require.Equal(t, tt.wantWS, s.codexCanonicalUAFor(ctx, upstreamWSWire))
		})
	}
}

func TestCodexNativeHTTPAndWSBridgeUseIndependentWireContracts(t *testing.T) {
	const clientVersion = "0.163.0"
	const serverVersion = "0.204.0"
	const clientUA = "codex_exec/0.163.0 (Linux) term (codex_exec; 0.163.0)"
	settings := NewSettingService(&codexVersionSettingRepoStub{values: map[string]string{
		SettingKeyOpenAICodexClientVersion: serverVersion, SettingKeyOpenAICodexClientVersionSynced: "0.170.0",
	}}, nil)
	svc := &OpenAIGatewayService{cfg: &config.Config{}, settingService: settings}
	account := newTestOAuthAccount(8123, map[string]any{"codex_fingerprint_mode": "full"})
	ids := resolveCodexFingerprintIDsFromRequest(account, nil)
	require.NotNil(t, ids)
	body := []byte(`{"model":"gpt-test","input":[]}`)
	for _, tc := range []struct {
		name, path string
		ingress    OpenAIClientTransport
		wire       codexWirePolicy
	}{
		{"direct HTTP", "/v1/responses", OpenAIClientTransportHTTP, forkHTTPWire},
		{"WS bridge", "/v1/responses", OpenAIClientTransportWS, upstreamWSWire},
		{"WS bridge with HTTP marker", "/v1/responses", OpenAIClientTransportHTTP, upstreamWSWire},
		{"WS bridge with HTTP compact context", "/v1/responses/compact", OpenAIClientTransportHTTP, upstreamWSWire},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, tc.path, nil)
			SetOpenAIClientTransport(c, tc.ingress)
			c.Request.Header.Set("user-agent", clientUA)
			c.Request.Header.Set("originator", "codex_exec")
			c.Request.Header.Set("version", clientVersion)
			stageCodexFingerprintIDs(c, ids)
			var req *http.Request
			var err error
			if tc.wire == forkHTTPWire {
				req, err = svc.buildUpstreamRequestOpenAIPassthrough(c.Request.Context(), c, account, body, "test-token")
			} else {
				req, err = svc.buildOpenAIPassthroughBaseRequest(c.Request.Context(), c, account, body, "test-token", upstreamWSWire)
			}
			require.NoError(t, err)
			t.Cleanup(func() { _ = req.Body.Close() })
			attemptBody, fixed := codexHTTPAttemptBody(req)
			if tc.wire == forkHTTPWire {
				require.True(t, fixed)
				require.NotEmpty(t, attemptBody)
				require.Equal(t, codexExecHTTPUserAgent(), req.UserAgent())
				require.Equal(t, codexExecHTTPVersion, req.Header.Get("version"))
				require.Equal(t, "zstd", req.Header.Get("content-encoding"))
				require.Empty(t, req.Header.Get("x-codex-installation-id"))
				require.Empty(t, req.Header.Get("session_id"))
			} else {
				require.False(t, fixed)
				require.False(t, HTTPUpstreamRedirectsDisabled(req.Context()))
				require.Equal(t, "chatgpt.com", req.URL.Host)
				require.Equal(t, "/backend-api/codex"+strings.TrimPrefix(tc.path, "/v1"), req.URL.Path)
				require.Equal(t, "codex_cli_rs", req.Header.Get("originator"))
				require.Equal(t, serverVersion, req.Header.Get("version"))
				require.Empty(t, req.Header.Get("content-encoding"))
				require.Equal(t, ids.installationID, req.Header.Get("x-codex-installation-id"))
				require.Equal(t, ids.sessionID, req.Header.Get("session_id"))
				got, err := io.ReadAll(req.Body)
				require.NoError(t, err)
				require.Equal(t, body, got)
			}
		})
	}
}

func TestCodexWSBridgeKeepsBoundPluginRouting(t *testing.T) {
	manager := &PluginManager{}
	manager.route.Store(&pluginRoute{pluginID: 1, rolloutPercent: 100, unavailable: "fixture bridge plugin offline"})
	upstream := &codexHTTPTransportRecorder{}
	svc := &OpenAIGatewayService{pluginManager: manager, httpUpstream: upstream}
	account := codexBodyEncodingTestAccount(332)
	req := httptest.NewRequest(http.MethodPost, chatgptCodexURL, nil)
	_, err := svc.doOpenAIWSHTTPBridgeUpstream(req, "", account)
	require.ErrorContains(t, err, "fixture bridge plugin offline")
	var configErr *CodexHTTPTransportConfigError
	require.NotErrorAs(t, err, &configErr)
	require.Empty(t, upstream.requests)
}
