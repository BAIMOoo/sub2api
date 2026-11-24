package service

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestCodexPinnedEgressVersionMeetsBundledMinimalClientVersions(t *testing.T) {
	pinned := NormalizeCodexClientVersion(codexExecHTTPVersion)
	require.NotEmpty(t, pinned, "pinned egress version must be a valid Codex client version")
	require.Equal(t, "0.171.0", pinned)

	bundled := openai.BundledCodexModelMinimalClientVersions()
	require.NotEmpty(t, bundled, "expected embedded Codex model manifests to declare minimal_client_version")

	for slug, rawMin := range bundled {
		minVer := NormalizeCodexClientVersion(rawMin)
		require.NotEmpty(t, minVer, "bundled model %s has invalid minimal_client_version %q", slug, rawMin)
		require.GreaterOrEqual(t, CompareVersions(pinned, minVer), 0,
			"pinned Codex egress version %s lags bundled model %s minimal_client_version %s",
			pinned, slug, minVer,
		)
	}
}

func TestCodexUpstreamFallbackVersionAndIsolationInvariants(t *testing.T) {
	// Keep the upstream compile-time fallback constant aligned with Wei-Shaw/sub2api.
	require.Equal(t, "0.146.0", codexCLIVersion)
	require.Equal(t, "0.144.0", codexUpstreamMinVersion)

	gin.SetMode(gin.TestMode)
	pinNativeHTTPTestIdentity(t)

	// Direct OpenAI API-key and WebSocket accounts must never inherit the pinned
	// 0.171.0 HTTP identity or accept-encoding header.
	for _, tc := range []struct {
		name      string
		transport OpenAIClientTransport
		account   *Account
	}{
		{
			name:      "api_key_direct_http",
			transport: OpenAIClientTransportHTTP,
			account: &Account{
				ID:          7101,
				Platform:    PlatformOpenAI,
				Type:        AccountTypeAPIKey,
				Credentials: map[string]any{"api_key": "sk-test"},
			},
		},
		{
			name:      "websocket_oauth",
			transport: OpenAIClientTransportWS,
			account:   codexBodyEncodingTestAccount(7102),
		},
		{
			name:      "third_party_grok_http",
			transport: OpenAIClientTransportHTTP,
			account: &Account{
				ID:          7103,
				Platform:    PlatformGrok,
				Type:        AccountTypeAPIKey,
				Credentials: map[string]any{"api_key": "xai-test"},
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
			c.Request.Header.Set("User-Agent", "noncodex-client/1.0")
			SetOpenAIClientTransport(c, tc.transport)

			svc := &OpenAIGatewayService{cfg: &config.Config{}}
			req, err := svc.buildUpstreamRequest(
				context.Background(), c, tc.account,
				[]byte(`{"model":"gpt-5.5","input":"hi","stream":true}`), "token", true, "", false,
			)
			require.NoError(t, err)
			require.NotEqual(t, codexExecHTTPVersion, req.Header.Get("version"))
			require.NotEqual(t, codexExecHTTPUserAgent(), req.Header.Get("User-Agent"))
			require.Empty(t, req.Header.Get("Accept-Encoding"))
		})
	}
}
