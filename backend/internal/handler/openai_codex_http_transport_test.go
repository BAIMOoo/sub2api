//go:build unit

package handler

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	middleware2 "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

type codexHTTPKeepaliveThenFailureUpstream struct {
	service.HTTPUpstream
	accountIDs []int64
}

func (u *codexHTTPKeepaliveThenFailureUpstream) Do(_ *http.Request, _ string, accountID int64, _ int) (*http.Response, error) {
	u.accountIDs = append(u.accountIDs, accountID)
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": {"text/event-stream"}},
		Body: io.NopCloser(strings.NewReader(
			"event: keepalive\ndata: {\"type\":\"keepalive\"}\n\n" +
				"data: {\"type\":\"response.failed\",\"response\":{\"id\":\"resp_first\",\"status\":\"failed\",\"error\":{\"message\":\"Our servers are currently overloaded. Please try again later.\"}}}\n\n",
		)),
	}, nil
}

func (u *codexHTTPKeepaliveThenFailureUpstream) DoWithTLS(req *http.Request, proxy string, accountID int64, concurrency int, _ *tlsfingerprint.Profile) (*http.Response, error) {
	return u.Do(req, proxy, accountID, concurrency)
}

func TestCodexHTTPConfigErrorAfterUpstreamKeepaliveStillReturns502(t *testing.T) {
	for _, firstType := range []string{service.AccountTypeOAuth, service.AccountTypeAPIKey} {
		t.Run(firstType, func(t *testing.T) {
			first := service.Account{
				ID: 1, Platform: service.PlatformOpenAI, Type: firstType,
				Status: service.StatusActive, Schedulable: true,
				Credentials: map[string]any{"access_token": "fixture", "api_key": "fixture"},
			}
			second := first
			second.ID, second.Priority, second.Type = 2, 1, service.AccountTypeOAuth
			second.Extra = map[string]any{"enable_tls_fingerprint": false}
			upstream := &codexHTTPKeepaliveThenFailureUpstream{}
			h := newOpenAIImagesSizeRoutingTestHandler(t, []service.Account{first, second}, upstream)

			rec, _ := serveOpenAIResponsesSizeRoutingRequest(h, 11, `{"model":"gpt-6-astra","input":"hello","stream":true}`)

			require.Equal(t, http.StatusBadGateway, rec.Code, rec.Body.String())
			require.Contains(t, gjson.Get(rec.Body.String(), "error.message").String(), "TLS fingerprint opt-out")
			require.NotEmpty(t, upstream.accountIDs)
			require.NotContains(t, upstream.accountIDs, int64(2), "the misconfigured account must not dispatch")
		})
	}
}

func TestOpenAIHTTPAPIKeyContinuationKeepsQueueHeartbeat(t *testing.T) {
	account := service.Account{
		ID: 1, Platform: service.PlatformOpenAI, Type: service.AccountTypeAPIKey,
		Status: service.StatusActive, Schedulable: true,
		Credentials: map[string]any{"api_key": "fixture"},
	}
	upstream := &openAIResponsesFailoverCancelUpstream{}
	h := newOpenAIImagesSizeRoutingTestHandler(t, []service.Account{account}, upstream)
	cache := &helperConcurrencyCacheStub{userSeq: []bool{false, true}, waitAllowed: true}
	h.concurrencyHelper = NewConcurrencyHelper(service.NewConcurrencyService(cache), SSEPingFormatComment, time.Millisecond)
	c, rec := newOpenAIResponsesFailoverTestContext(t, t.Context())
	c.Request = httptest.NewRequestWithContext(t.Context(), http.MethodPost, "/v1/responses", strings.NewReader(
		`{"model":"gpt-5.1","input":"hello","previous_response_id":"resp_previous","stream":true}`,
	))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Set(string(middleware2.ContextKeyUser), middleware2.AuthSubject{UserID: 100, Concurrency: 1})
	apiKey, ok := middleware2.GetAPIKeyFromContext(c)
	require.True(t, ok)
	require.NoError(t, h.gatewayService.BindOpenAIHTTPResponseOwner(t.Context(), *apiKey.GroupID, "resp_previous", 100, apiKey.ID))

	h.Responses(c)

	require.Equal(t, 2, cache.userAcquireCalls, rec.Body.String())
	require.Equal(t, []int64{1}, upstream.calls())
	require.True(t, strings.HasPrefix(rec.Body.String(), string(SSEPingFormatComment)), rec.Body.String())
}

func TestCodexHTTPConfigErrorAfterWaitingStillReturns502(t *testing.T) {
	for _, tc := range []struct {
		path, body string
		handle     func(*OpenAIGatewayHandler, *gin.Context)
	}{
		{"/v1/responses", `{"model":"gpt-6-astra","input":"hello","stream":true}`, (*OpenAIGatewayHandler).Responses},
		{"/v1/responses/compact", `{"model":"gpt-6-astra","input":[{"role":"user","content":"hello"}],"stream":true}`, (*OpenAIGatewayHandler).Responses},
		{"/v1/chat/completions", `{"model":"gpt-6-astra","messages":[{"role":"user","content":"hello"}],"stream":true}`, (*OpenAIGatewayHandler).ChatCompletions},
		{"/v1/messages", `{"model":"gpt-6-astra","max_tokens":32,"messages":[{"role":"user","content":"hello"}],"stream":true}`, (*OpenAIGatewayHandler).Messages},
	} {
		t.Run(tc.path, func(t *testing.T) {
			account := service.Account{
				ID: 1, Platform: service.PlatformOpenAI, Type: service.AccountTypeOAuth,
				Status: service.StatusActive, Schedulable: true,
				Credentials: map[string]any{"access_token": "fixture"},
				Extra:       map[string]any{"enable_tls_fingerprint": false},
			}
			upstream := &openAIResponsesFailoverCancelUpstream{}
			h := newOpenAIImagesSizeRoutingTestHandler(t, []service.Account{account}, upstream)
			cache := &helperConcurrencyCacheStub{userSeq: []bool{false, true}, waitAllowed: true}
			h.concurrencyHelper = NewConcurrencyHelper(service.NewConcurrencyService(cache), SSEPingFormatComment, time.Millisecond)
			groupID := int64(11)
			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			c.Request = httptest.NewRequestWithContext(t.Context(), http.MethodPost, tc.path, strings.NewReader(tc.body))
			c.Request.Header.Set("Content-Type", "application/json")
			c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
				ID: 99, GroupID: &groupID, Group: &service.Group{ID: groupID, Platform: service.PlatformOpenAI, AllowMessagesDispatch: true},
				User: &service.User{ID: 100},
			})
			c.Set(string(middleware2.ContextKeyUser), middleware2.AuthSubject{UserID: 100, Concurrency: 1})

			tc.handle(h, c)

			require.Equal(t, 2, cache.userAcquireCalls)
			require.Empty(t, upstream.calls())
			require.Equal(t, http.StatusBadGateway, rec.Code, rec.Body.String())
			require.Contains(t, gjson.Get(rec.Body.String(), "error.message").String(), "TLS fingerprint opt-out")
		})
	}
}
