package service

import (
	"context"
	"net/http"
	"net/http/httptest"
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
	for _, ingress := range []OpenAIClientTransport{OpenAIClientTransportHTTP, OpenAIClientTransportWS} {
		t.Run(string(ingress), func(t *testing.T) {
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
			SetOpenAIClientTransport(c, ingress)
			c.Request.Header.Set("user-agent", clientUA)
			c.Request.Header.Set("originator", "codex_exec")
			c.Request.Header.Set("version", clientVersion)
			stageCodexFingerprintIDs(c, ids)
			var req *http.Request
			var err error
			if ingress == OpenAIClientTransportHTTP {
				req, err = svc.buildUpstreamRequestOpenAIPassthrough(c.Request.Context(), c, account, body, "test-token")
			} else {
				req, err = svc.buildOpenAIPassthroughBaseRequest(c.Request.Context(), c, account, body, "test-token", upstreamWSWire)
			}
			require.NoError(t, err)
			t.Cleanup(func() { _ = req.Body.Close() })
			if ingress == OpenAIClientTransportHTTP {
				require.Equal(t, codexExecHTTPUserAgent(), req.UserAgent())
				require.Equal(t, codexExecHTTPVersion, req.Header.Get("version"))
				require.Equal(t, "zstd", req.Header.Get("content-encoding"))
				require.Empty(t, req.Header.Get("x-codex-installation-id"))
				require.Empty(t, req.Header.Get("session_id"))
			} else {
				require.Equal(t, "codex_cli_rs", req.Header.Get("originator"))
				require.Equal(t, serverVersion, req.Header.Get("version"))
				require.Empty(t, req.Header.Get("content-encoding"))
				require.Equal(t, ids.installationID, req.Header.Get("x-codex-installation-id"))
				require.Equal(t, ids.sessionID, req.Header.Get("session_id"))
			}
		})
	}
}
