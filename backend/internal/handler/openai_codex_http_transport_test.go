//go:build unit

package handler

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	middleware2 "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

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
