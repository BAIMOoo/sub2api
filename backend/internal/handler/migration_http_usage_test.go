package handler

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func migrationHTTPTestState(t *testing.T, protocol MigrationCompletionProtocol) (*gin.Context, *httptest.ResponseRecorder, *service.MigrationUsageJournal, *migrationHTTPUsage) {
	t.Helper()
	sink := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(sink)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	journal, err := service.OpenMigrationUsageJournal(t.TempDir())
	require.NoError(t, err)
	t.Cleanup(func() { require.NoError(t, journal.Close()) })
	state, err := installMigrationHTTPUsage(c, journal, service.MigrationUsageCaptureOptions{}, MigrationCompletionOptions{
		Protocol: protocol, MemoryBytes: 64, MaxFrameBytes: 64 * 1024, MaxHeldBytes: 1024 * 1024, SpoolDirectory: t.TempDir(),
	})
	require.NoError(t, err)
	t.Cleanup(state.Close)
	return c, sink, journal, state
}

func migrationHTTPTestBillingService() *service.OpenAIGatewayService {
	cfg := &config.Config{}
	cfg.Default.RateMultiplier = 1
	return service.NewOpenAIGatewayService(nil, nil, nil, nil, nil, nil, nil, cfg, nil, nil,
		service.NewBillingService(cfg, nil), nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil)
}

func migrationHTTPTestRecord(t *testing.T, ctx context.Context, requestID string) error {
	t.Helper()
	return migrationHTTPTestBillingService().RecordUsage(ctx, &service.OpenAIRecordUsageInput{
		Result: &service.OpenAIForwardResult{RequestID: requestID, Model: "gpt-5.4-nano", Usage: service.OpenAIUsage{InputTokens: 100, OutputTokens: 20}},
		APIKey: &service.APIKey{ID: 2}, User: &service.User{ID: 1}, Account: &service.Account{ID: 3},
	})
}

func TestMigrationHTTPUsageHoldsEachProtocolUntilActualUsageIsDurable(t *testing.T) {
	for _, test := range []struct {
		name            string
		protocol        MigrationCompletionProtocol
		delta, terminal string
	}{
		{"responses", MigrationCompletionResponses, "event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"hello\"}\n\n", "event: response.completed\ndata: {\"type\":\"response.completed\"}\n\n"},
		{"chat", MigrationCompletionChat, "data: {\"choices\":[{\"delta\":{\"content\":\"hello\"},\"finish_reason\":null}]}\n\n", "data: [DONE]\n\n"},
		{"messages", MigrationCompletionMessages, "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"delta\":{\"type\":\"text_delta\",\"text\":\"hello\"}}\n\n", "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"},
		{"nonstream", MigrationCompletionNonStreaming, "", `{"id":"resp_held","output":"hello"}`},
	} {
		t.Run(test.name, func(t *testing.T) {
			c, sink, journal, state := migrationHTTPTestState(t, test.protocol)
			_, err := c.Writer.WriteString(test.delta + test.terminal)
			require.NoError(t, err)
			require.Equal(t, test.delta, sink.Body.String())
			c.Writer.Flush()
			require.NotContains(t, sink.Body.String(), test.terminal)
			h := &OpenAIGatewayHandler{}
			h.submitUsageRecordTask(c.Request.Context(), func(ctx context.Context) {
				require.NotContains(t, sink.Body.String(), test.terminal)
				require.False(t, state.writer.PersistenceConfirmed())
				require.NoError(t, migrationRecordUsageResult(ctx, migrationHTTPTestRecord(t, ctx, "protocol-"+test.name)))
				pending, err := journal.Pending()
				require.NoError(t, err)
				require.Len(t, pending, 1)
				require.NotContains(t, sink.Body.String(), test.terminal, "fsync must precede client confirmation")
			})
			require.NoError(t, state.err)
			require.True(t, state.writer.PersistenceConfirmed())
			require.Equal(t, test.delta+test.terminal, sink.Body.String())
		})
	}
}

func TestMigrationHTTPUsageMissingProofCannotBecomeGinEmpty200(t *testing.T) {
	c, sink, journal, state := migrationHTTPTestState(t, MigrationCompletionNonStreaming)
	_, err := c.Writer.WriteString(`{"unsafe":"success"}`)
	require.NoError(t, err)
	(&GatewayHandler{}).submitUsageRecordTask(c.Request.Context(), func(context.Context) {})
	state.Close()
	require.Equal(t, http.StatusServiceUnavailable, sink.Code)
	require.NotContains(t, sink.Body.String(), "unsafe")
	require.False(t, state.writer.PersistenceConfirmed())
	pending, err := journal.Pending()
	require.NoError(t, err)
	require.Empty(t, pending)
}

func TestMigrationHTTPUsageReturnedBillingErrorBlocksEvenPersistedProof(t *testing.T) {
	c, sink, journal, state := migrationHTTPTestState(t, MigrationCompletionNonStreaming)
	_, err := c.Writer.WriteString(`{"unsafe":"success"}`)
	require.NoError(t, err)
	(&OpenAIGatewayHandler{}).submitMandatoryUsageRecordTask(c.Request.Context(), func(ctx context.Context) {
		require.NoError(t, migrationHTTPTestRecord(t, ctx, "returned-error"))
		migrationRecordUsageResult(ctx, errors.New("later accounting failure"))
	})
	require.ErrorContains(t, state.err, "later accounting failure")
	require.Equal(t, http.StatusServiceUnavailable, sink.Code)
	require.NotContains(t, sink.Body.String(), "unsafe")
	pending, err := journal.Pending()
	require.NoError(t, err)
	require.Len(t, pending, 1, "never delete an intent on an unknown final outcome")
}

func TestMigrationHTTPUsagePanicNeverFallsBackToLegacyAccounting(t *testing.T) {
	for _, test := range []struct {
		name   string
		submit func(context.Context, service.UsageRecordTask)
	}{
		{"gateway", (&GatewayHandler{}).submitUsageRecordTask},
		{"gateway-mandatory", (&GatewayHandler{}).submitMandatoryUsageRecordTask},
		{"openai", (&OpenAIGatewayHandler{}).submitUsageRecordTask},
		{"openai-mandatory", (&OpenAIGatewayHandler{}).submitMandatoryUsageRecordTask},
	} {
		t.Run(test.name, func(t *testing.T) {
			c, sink, journal, state := migrationHTTPTestState(t, MigrationCompletionNonStreaming)
			_, err := c.Writer.WriteString(`{"unsafe":"success"}`)
			require.NoError(t, err)
			calls := 0
			test.submit(c.Request.Context(), func(ctx context.Context) {
				calls++
				if calls == 1 {
					require.NoError(t, migrationHTTPTestRecord(t, ctx, "panic-"+test.name))
				}
				panic("accounting panic after durable intent")
			})
			require.Equal(t, 1, calls, "a handled capture request must never resubmit legacy accounting")
			require.ErrorContains(t, state.err, "accounting panic")
			require.Equal(t, http.StatusServiceUnavailable, sink.Code)
			require.NotContains(t, sink.Body.String(), "unsafe")
			require.False(t, state.writer.PersistenceConfirmed())
			pending, err := journal.Pending()
			require.NoError(t, err)
			require.Len(t, pending, 1)
		})
	}
}

func TestMigrationHTTPUsageClientCancellationKeepsCaptureAndRequestValues(t *testing.T) {
	c, sink, _, state := migrationHTTPTestState(t, MigrationCompletionNonStreaming)
	ctx, cancel := context.WithCancel(c.Request.Context())
	c.Request = c.Request.WithContext(ctx)
	cancel()
	_, err := c.Writer.WriteString(`{"id":"completed"}`)
	require.NoError(t, err)
	(&GatewayHandler{}).submitMandatoryUsageRecordTask(c.Request.Context(), func(ctx context.Context) {
		require.NoError(t, ctx.Err())
		require.NoError(t, migrationRecordUsageResult(ctx, migrationHTTPTestRecord(t, ctx, "cancelled-client")))
	})
	require.NoError(t, state.err)
	require.Equal(t, `{"id":"completed"}`, sink.Body.String())
}

func TestMigrationHTTPUsageEarlyErrorPreservesOriginalStatusAndBody(t *testing.T) {
	c, sink, _, state := migrationHTTPTestState(t, MigrationCompletionNonStreaming)
	c.JSON(http.StatusTooManyRequests, gin.H{"error": gin.H{"message": "existing limit"}})
	state.Close()
	require.Equal(t, http.StatusTooManyRequests, sink.Code)
	require.Contains(t, sink.Body.String(), "existing limit")
	require.NotContains(t, sink.Body.String(), "Usage persistence")
}

func TestMigrationHTTPUsageConfiguredWithoutRuntimeFailsBeforeForward(t *testing.T) {
	t.Setenv("MIGRATION_USAGE_ENABLED", "invalid")
	sink := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(sink)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	state, ok := prepareMigrationHTTPUsage(c, MigrationCompletionResponses, true, false)
	require.False(t, ok)
	require.Nil(t, state)
	require.Equal(t, http.StatusServiceUnavailable, sink.Code)
	require.True(t, c.IsAborted())
}

type migrationHTTPUserRepo struct{ service.UserRepository }

func (r *migrationHTTPUserRepo) GetByID(context.Context, int64) (*service.User, error) {
	return &service.User{ID: 1, Balance: 100, Status: service.StatusActive}, nil
}

func (r *migrationHTTPUserRepo) DeductBalance(context.Context, int64, float64) error {
	panic("legacy balance deduction must never run under migration capture")
}

type migrationHTTPUpstream struct{ service.HTTPUpstream }

func (u *migrationHTTPUpstream) Do(req *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
	body, _ := io.ReadAll(req.Body)
	streaming := gjson.GetBytes(body, "stream").Bool()
	response := `{"id":"resp_migration","object":"response","model":"gpt-5.4-nano","status":"completed","output":[{"id":"msg_migration","type":"message","role":"assistant","status":"completed","content":[{"type":"output_text","text":"hello","annotations":[]}]}],"usage":{"input_tokens":100,"output_tokens":20,"total_tokens":120}}`
	contentType := "application/json"
	if strings.Contains(req.URL.Path, "chat/completions") {
		response = `{"id":"chatcmpl_migration","object":"chat.completion","model":"gpt-5.4-nano","choices":[{"index":0,"message":{"role":"assistant","content":"hello"},"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120}}`
		if streaming {
			response = "data: {\"id\":\"chatcmpl_migration\",\"model\":\"gpt-5.4-nano\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hello\"},\"finish_reason\":null}]}\n\n" +
				"data: {\"id\":\"chatcmpl_migration\",\"model\":\"gpt-5.4-nano\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}],\"usage\":{\"prompt_tokens\":100,\"completion_tokens\":20,\"total_tokens\":120}}\n\ndata: [DONE]\n\n"
		}
	} else if streaming {
		response = "event: response.created\ndata: {\"type\":\"response.created\",\"response\":{\"id\":\"resp_migration\",\"model\":\"gpt-5.4-nano\"}}\n\n" +
			"event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"hello\",\"output_index\":0,\"content_index\":0}\n\n" +
			"event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":" + response + "}\n\n"
	}
	if streaming {
		contentType = "text/event-stream"
	}
	return &http.Response{StatusCode: http.StatusOK, Header: http.Header{"Content-Type": []string{contentType}}, Body: io.NopCloser(strings.NewReader(response))}, nil
}

func TestMigrationHTTPUsageRealOpenAIHandlersPersistBeforeReturn(t *testing.T) {
	t.Setenv("MIGRATION_USAGE_ENABLED", "") // scoped injection avoids replacing the startup-only global runtime
	for _, test := range []struct {
		name, path, body string
		protocol         MigrationCompletionProtocol
	}{
		{"responses", "/openai/v1/responses", `{"model":"gpt-5.4-nano","input":"hello","stream":false}`, MigrationCompletionNonStreaming},
		{"responses-stream", "/openai/v1/responses", `{"model":"gpt-5.4-nano","input":"hello","stream":true}`, MigrationCompletionResponses},
		{"chat", "/v1/chat/completions", `{"model":"gpt-5.4-nano","messages":[{"role":"user","content":"hello"}],"stream":false}`, MigrationCompletionNonStreaming},
		{"chat-stream", "/v1/chat/completions", `{"model":"gpt-5.4-nano","messages":[{"role":"user","content":"hello"}],"stream":true}`, MigrationCompletionChat},
		{"messages", "/v1/messages", `{"model":"gpt-5.4-nano","max_tokens":200,"messages":[{"role":"user","content":"hello"}],"stream":false}`, MigrationCompletionNonStreaming},
		{"messages-stream", "/v1/messages", `{"model":"gpt-5.4-nano","max_tokens":200,"messages":[{"role":"user","content":"hello"}],"stream":true}`, MigrationCompletionMessages},
	} {
		t.Run(test.name, func(t *testing.T) {
			cfg := &config.Config{}
			cfg.Default.RateMultiplier = 1
			cfg.Security.URLAllowlist.Enabled = false
			userRepo := &migrationHTTPUserRepo{}
			billingCache := service.NewBillingCacheService(nil, userRepo, nil, nil, nil, nil, cfg, nil)
			t.Cleanup(billingCache.Stop)
			accountRepo := &openAIWSUsageHandlerAccountRepoStub{account: service.Account{ID: 3, Name: "isolated", Platform: service.PlatformOpenAI,
				Type: service.AccountTypeAPIKey, Status: service.StatusActive, Schedulable: true,
				Credentials: map[string]any{"api_key": "isolated-fake-upstream-key", "base_url": "https://api.example.test"},
				Extra:       map[string]any{"openai_passthrough": true}}}
			gateway := service.NewOpenAIGatewayService(accountRepo, nil, nil, userRepo, nil, nil, nil, cfg, nil, nil,
				service.NewBillingService(cfg, nil), nil, billingCache, &migrationHTTPUpstream{}, &service.DeferredService{}, nil, nil, nil, nil, nil, nil, nil)
			h := NewOpenAIGatewayHandler(gateway, service.NewConcurrencyService(nil), billingCache,
				service.NewAPIKeyService(nil, nil, nil, nil, nil, nil, cfg), nil, nil, nil, nil, cfg)
			c, sink, journal, state := migrationHTTPTestState(t, test.protocol)
			c.Request = httptest.NewRequest(http.MethodPost, test.path, strings.NewReader(test.body)).WithContext(c.Request.Context())
			c.Request.Header.Set("Content-Type", "application/json")
			groupID := int64(5)
			c.Set(string(middleware.ContextKeyAPIKey), &service.APIKey{ID: 2, GroupID: &groupID,
				User: &service.User{ID: 1, Balance: 100, Status: service.StatusActive}, Group: &service.Group{ID: groupID, Platform: service.PlatformOpenAI, Status: service.StatusActive, RateMultiplier: 1, AllowMessagesDispatch: true}})
			c.Set(string(middleware.ContextKeyUser), middleware.AuthSubject{UserID: 1})
			switch {
			case strings.Contains(test.path, "chat/completions"):
				h.ChatCompletions(c)
			case strings.Contains(test.path, "messages"):
				h.Messages(c)
			default:
				h.Responses(c)
			}
			state.Close()
			require.NoError(t, state.err, "response: %s", sink.Body.String())
			require.True(t, state.writer.PersistenceConfirmed(), "response: %s", sink.Body.String())
			require.Equal(t, http.StatusOK, sink.Code)
			require.Contains(t, sink.Body.String(), "hello")
			pending, err := journal.Pending()
			require.NoError(t, err)
			require.Len(t, pending, 1)
			require.Equal(t, "gpt-5.4-nano", pending[0].Pricing.BillingModel)
			require.Positive(t, pending[0].Command.BalanceCost)
			require.WithinDuration(t, time.Now(), pending[0].Usage.CreatedAt, time.Minute)
		})
	}
}
