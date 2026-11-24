//go:build unit

package service

import (
	"bytes"
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestRateLimitService_HandleUpstreamError_CodexClientVersionMismatchSkipsCooldownAndFailover(t *testing.T) {
	cases := []struct {
		name string
		body string
	}{
		{
			name: "explicit client version too old",
			body: `{"detail":"Your Codex client version 0.158.0 is too old for this model. Please upgrade Codex to 0.166.0 or newer."}`,
		},
		{
			name: "plan-gated phrase combined with explicit version requirement",
			body: `{"detail":"The 'gpt-6.2-sol' model is not supported when using Codex with a ChatGPT account. Minimum client version 0.166.0 is required."}`,
		},
		{
			name: "structured unsupported client version error",
			body: `{"error":{"type":"invalid_request_error","message":"Unsupported client version; please update Codex client."}}`,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			previous := slog.Default()
			var logs bytes.Buffer
			slog.SetDefault(slog.New(slog.NewTextHandler(&logs, nil)))
			t.Cleanup(func() { slog.SetDefault(previous) })

			repo := &modelNotFoundAccountRepoStub{}
			svc := &RateLimitService{accountRepo: repo}
			account := openAICodexPlanGatedOAuthAccount()

			handled := svc.HandleUpstreamError(
				context.Background(),
				account,
				http.StatusBadRequest,
				http.Header{},
				[]byte(tc.body),
				"gpt-6.2-sol",
			)

			require.False(t, handled, "explicit client-version 400 must not trigger account failover")
			require.Empty(t, repo.modelRateLimitCalls, "explicit client-version 400 must not cool down the account")
			require.Zero(t, repo.tempCalls)
			require.Contains(t, logs.String(), "codex_client_version_mismatch_cooldown_skipped")
		})
	}
}

func TestRateLimitService_HandleUpstreamError_CodexFixedHTTPVersionLagFromAccountMetadataSkipsCooldown(t *testing.T) {
	previous := slog.Default()
	var logs bytes.Buffer
	slog.SetDefault(slog.New(slog.NewTextHandler(&logs, nil)))
	t.Cleanup(func() { slog.SetDefault(previous) })

	repo := &modelNotFoundAccountRepoStub{}
	svc := &RateLimitService{accountRepo: repo}
	account := openAICodexPlanGatedOAuthAccount()
	account.SetUpstreamModelMetadataSnapshot(UpstreamModelMetadataSnapshot{
		Source:   "upstream",
		SyncedAt: time.Now().UTC().Format(time.RFC3339),
		Models: map[string]UpstreamModelMetadata{
			"gpt-6.9-future": {
				ID:                   "gpt-6.9-future",
				MinimalClientVersion: "0.999.0",
			},
		},
	})

	handled := svc.HandleUpstreamError(
		withCodexFixedHTTPEgress(context.Background()),
		account,
		http.StatusBadRequest,
		http.Header{},
		[]byte(`{"detail":"The 'gpt-6.9-future' model is not supported when using Codex with a ChatGPT account."}`),
		"gpt-6.9-future",
	)

	require.False(t, handled, "known fixed-HTTP version lag must not trigger pool failover")
	require.Empty(t, repo.modelRateLimitCalls, "known fixed-HTTP version lag must not cool down the account")
	require.Zero(t, repo.tempCalls)
	require.Contains(t, logs.String(), "codex_fixed_http_client_version_lag_cooldown_skipped")
	require.Contains(t, logs.String(), "minimal_client_version=0.999.0")
	require.Contains(t, logs.String(), "source=account_metadata")
}

func TestRateLimitService_HandleUpstreamError_CodexFixedHTTPVersionLagFromBundledManifestViaMapping(t *testing.T) {
	account := openAICodexPlanGatedOAuthAccount()
	account.Credentials["model_mapping"] = map[string]any{"sol-alias": "gpt-6.2-sol"}

	minVersion, source := codexModelMinimalClientVersion(account, "sol-alias", "gpt-6.2-sol")
	require.Equal(t, "0.166.0", minVersion)
	require.Equal(t, "bundled_manifest", source)
	require.True(t, codexVersionLagsRequirement("0.158.0", minVersion))
	require.False(t, codexVersionLagsRequirement("0.171.0", minVersion))

	// On any pinned egress version below 0.166.0, HandleUpstreamError must skip cooldown & failover
	// using the bundled manifest floor without requiring an account snapshot.
	if CompareVersions(codexExecHTTPVersion, "0.166.0") < 0 {
		previous := slog.Default()
		var logs bytes.Buffer
		slog.SetDefault(slog.New(slog.NewTextHandler(&logs, nil)))
		t.Cleanup(func() { slog.SetDefault(previous) })

		repo := &modelNotFoundAccountRepoStub{}
		svc := &RateLimitService{accountRepo: repo}
		handled := svc.HandleUpstreamError(
			withCodexFixedHTTPEgress(context.Background()),
			account,
			http.StatusBadRequest,
			http.Header{},
			[]byte(`{"detail":"The 'gpt-6.2-sol' model is not supported when using Codex with a ChatGPT account."}`),
			"sol-alias",
		)
		require.False(t, handled)
		require.Empty(t, repo.modelRateLimitCalls)
		require.Contains(t, logs.String(), "source=bundled_manifest")
	}
}

func TestRateLimitService_HandleUpstreamError_CodexFixedHTTPGenuinePlanGatedKeepsCooldown(t *testing.T) {
	repo := &modelNotFoundAccountRepoStub{}
	svc := &RateLimitService{accountRepo: repo}
	account := openAICodexPlanGatedOAuthAccount()
	account.SetUpstreamModelMetadataSnapshot(UpstreamModelMetadataSnapshot{
		Source:   "upstream",
		SyncedAt: time.Now().UTC().Format(time.RFC3339),
		Models: map[string]UpstreamModelMetadata{
			"gpt-6.1-sol": {
				ID:                   "gpt-6.1-sol",
				MinimalClientVersion: "0.153.0",
			},
		},
	})

	handled := svc.HandleUpstreamError(
		withCodexFixedHTTPEgress(context.Background()),
		account,
		http.StatusBadRequest,
		http.Header{},
		[]byte(`{"detail":"The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account."}`),
		"gpt-6.1-sol",
	)

	require.True(t, handled, "genuine plan-gated 400 when version satisfies floor must keep failover")
	require.Len(t, repo.modelRateLimitCalls, 1)
	require.Equal(t, "gpt-6.1-sol", repo.modelRateLimitCalls[0].scope)
	require.Equal(t, upstreamCodexPlanGatedModelReason, repo.modelRateLimitCalls[0].reason)
	require.WithinDuration(t, time.Now().Add(upstreamCodexPlanGatedModelCooldown), repo.modelRateLimitCalls[0].resetAt, 5*time.Second)
}

func TestRateLimitService_HandleUpstreamError_NonFixedHTTPKeepsPlanGatedCooldownDespiteHighMinimalVersion(t *testing.T) {
	repo := &modelNotFoundAccountRepoStub{}
	svc := &RateLimitService{accountRepo: repo}
	account := openAICodexPlanGatedOAuthAccount()
	account.SetUpstreamModelMetadataSnapshot(UpstreamModelMetadataSnapshot{
		Source:   "upstream",
		SyncedAt: time.Now().UTC().Format(time.RFC3339),
		Models: map[string]UpstreamModelMetadata{
			"gpt-6.9-future": {
				ID:                   "gpt-6.9-future",
				MinimalClientVersion: "0.999.0",
			},
		},
	})

	// Without fixed-HTTP context marker (e.g. WebSocket / legacy transport),
	// Guard 2 does not apply because that transport does not use codexExecHTTPVersion.
	handled := svc.HandleUpstreamError(
		context.Background(),
		account,
		http.StatusBadRequest,
		http.Header{},
		[]byte(`{"detail":"The 'gpt-6.9-future' model is not supported when using Codex with a ChatGPT account."}`),
		"gpt-6.9-future",
	)

	require.True(t, handled)
	require.Len(t, repo.modelRateLimitCalls, 1)
	require.Equal(t, upstreamCodexPlanGatedModelReason, repo.modelRateLimitCalls[0].reason)
}

func TestExtractUpstreamModelCatalogPreservesMinimalClientVersion(t *testing.T) {
	body := []byte(`{"models":[{"slug":"gpt-6.2-sol","display_name":"GPT-6.2-Sol","minimal_client_version":"0.166.0","context_window":500000,"input_modalities":["text"],"supported_reasoning_levels":[{"effort":"low"}]}]}`)
	models, metadata, err := extractUpstreamModelCatalog(body, false)
	require.NoError(t, err)
	require.Equal(t, []string{"gpt-6.2-sol"}, models)
	require.Equal(t, "0.166.0", metadata["gpt-6.2-sol"].MinimalClientVersion)
}

func TestOpenAIGateway_FixedHTTP400VersionLagSurfaces400WithoutCooldownOrFailover(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const upstream400 = `{"detail":"The 'gpt-6.9-future' model is not supported when using Codex with a ChatGPT account."}`

	for _, tc := range []struct {
		name        string
		path        string
		body        string
		passthrough bool
		call        func(svc *OpenAIGatewayService, c *gin.Context, account *Account, body []byte) error
	}{
		{
			name: "responses",
			path: "/v1/responses",
			body: `{"model":"gpt-6.9-future","input":"hi","stream":false}`,
			call: func(svc *OpenAIGatewayService, c *gin.Context, account *Account, body []byte) error {
				_, err := svc.Forward(c.Request.Context(), c, account, body)
				return err
			},
		},
		{
			name:        "passthrough",
			path:        "/v1/responses",
			body:        `{"model":"gpt-6.9-future","input":"hi","stream":false}`,
			passthrough: true,
			call: func(svc *OpenAIGatewayService, c *gin.Context, account *Account, body []byte) error {
				_, err := svc.Forward(c.Request.Context(), c, account, body)
				return err
			},
		},
		{
			name: "chat_completions",
			path: "/v1/chat/completions",
			body: `{"model":"gpt-6.9-future","messages":[{"role":"user","content":"hi"}],"stream":false}`,
			call: func(svc *OpenAIGatewayService, c *gin.Context, account *Account, body []byte) error {
				_, err := svc.ForwardAsChatCompletions(c.Request.Context(), c, account, body, "", "")
				return err
			},
		},
		{
			name: "messages",
			path: "/v1/messages",
			body: `{"model":"gpt-6.9-future","max_tokens":32,"messages":[{"role":"user","content":"hi"}],"stream":false}`,
			call: func(svc *OpenAIGatewayService, c *gin.Context, account *Account, body []byte) error {
				_, err := svc.ForwardAsAnthropic(c.Request.Context(), c, account, body, "", "")
				return err
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			repo := &modelNotFoundAccountRepoStub{}
			rateLimit := &RateLimitService{accountRepo: repo}
			upstream := &httpUpstreamRecorder{resp: &http.Response{
				StatusCode: http.StatusBadRequest,
				Header:     http.Header{"Content-Type": []string{"application/json"}},
				Body:       io.NopCloser(strings.NewReader(upstream400)),
			}}
			svc := &OpenAIGatewayService{
				cfg:              &config.Config{},
				httpUpstream:     upstream,
				rateLimitService: rateLimit,
			}
			account := codexBodyEncodingTestAccount(158)
			account.Extra = map[string]any{"openai_oauth_passthrough": tc.passthrough}
			account.SetUpstreamModelMetadataSnapshot(UpstreamModelMetadataSnapshot{
				Source:   "upstream",
				SyncedAt: time.Now().UTC().Format(time.RFC3339),
				Models: map[string]UpstreamModelMetadata{
					"gpt-6.9-future": {
						ID:                   "gpt-6.9-future",
						MinimalClientVersion: "0.999.0",
					},
				},
			})

			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			c.Request = httptest.NewRequest(http.MethodPost, tc.path, strings.NewReader(tc.body))
			c.Request.Header.Set("Content-Type", "application/json")
			c.Request.Header.Set("User-Agent", "noncodex-harness/1")
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)

			err := tc.call(svc, c, account, []byte(tc.body))
			require.Error(t, err)
			var failoverErr *UpstreamFailoverError
			require.NotErrorAs(t, err, &failoverErr, "version-lag 400 must not trigger account failover")
			require.Equal(t, http.StatusBadRequest, rec.Code, "upstream 400 must surface directly to client")
			require.Empty(t, repo.modelRateLimitCalls, "account must not be cooled down for 30 minutes")
		})
	}
}
