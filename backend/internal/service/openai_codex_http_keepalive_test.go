package service

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

type codexHTTPKeepaliveAccountRepo struct {
	AccountRepository
	schedulable []Account
	configured  []Account
}

func (r codexHTTPKeepaliveAccountRepo) ListSchedulableByPlatform(context.Context, string) ([]Account, error) {
	return r.schedulable, nil
}

func (r codexHTTPKeepaliveAccountRepo) ListModelAvailabilityCandidates(context.Context, *int64, []string, bool) ([]Account, error) {
	return r.configured, nil
}

func TestCodexHTTPConfigErrorKeepaliveConsidersCoolingAccounts(t *testing.T) {
	apiKey := Account{ID: 1, Platform: PlatformOpenAI, Type: AccountTypeAPIKey, Status: StatusActive, Schedulable: true}
	resumesAt := time.Now().Add(time.Minute)
	codex := Account{
		ID: 2, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Status: StatusActive, Schedulable: true,
		TempUnschedulableUntil: &resumesAt,
	}
	require.False(t, codex.IsSchedulable())
	for _, tc := range []struct {
		name       string
		configured []Account
		deferPing  bool
	}{
		{"API Key only", []Account{apiKey}, false},
		{"Codex can recover before failover", []Account{apiKey, codex}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			svc := &OpenAIGatewayService{
				cfg: &config.Config{RunMode: config.RunModeSimple},
				accountRepo: codexHTTPKeepaliveAccountRepo{
					schedulable: []Account{apiKey},
					configured:  tc.configured,
				},
			}
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
			c.Set("api_key", &APIKey{ID: 42})
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			svc.DeferOpenAIHTTPPreOutputKeepalive(c, false)

			require.Equal(t, tc.deferPing, svc.openAIHTTPPreOutputKeepaliveDeferred(c, &apiKey))
		})
	}
}
