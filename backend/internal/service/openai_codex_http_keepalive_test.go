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
	active      []Account
	members     []Account
}

func (r codexHTTPKeepaliveAccountRepo) ListSchedulableByPlatform(context.Context, string) ([]Account, error) {
	return r.schedulable, nil
}

func (r codexHTTPKeepaliveAccountRepo) ListModelAvailabilityCandidates(context.Context, *int64, []string, bool) ([]Account, error) {
	return r.active, nil
}

func (r codexHTTPKeepaliveAccountRepo) ListAllWithFilters(context.Context, string, string, string, string, int64, string) ([]Account, error) {
	return r.members, nil
}

func TestCodexHTTPConfigErrorKeepaliveConsidersRecoverableAccounts(t *testing.T) {
	apiKey := Account{ID: 1, Platform: PlatformOpenAI, Type: AccountTypeAPIKey, Status: StatusActive, Schedulable: true}
	resumesAt := time.Now().Add(time.Minute)
	codex := Account{
		ID: 2, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Status: StatusActive, Schedulable: true,
		TempUnschedulableUntil: &resumesAt,
	}
	require.False(t, codex.IsSchedulable())
	errored := codex
	errored.Status = StatusError
	errored.TempUnschedulableUntil = nil
	for _, tc := range []struct {
		name      string
		active    []Account
		members   []Account
		deferPing bool
	}{
		{"API Key only", []Account{apiKey}, []Account{apiKey}, false},
		{"Codex in cooldown", []Account{apiKey, codex}, []Account{apiKey, codex}, true},
		{"Codex in error status", []Account{apiKey}, []Account{apiKey, errored}, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			svc := &OpenAIGatewayService{
				cfg: &config.Config{RunMode: config.RunModeSimple},
				accountRepo: codexHTTPKeepaliveAccountRepo{
					schedulable: []Account{apiKey},
					active:      tc.active,
					members:     tc.members,
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
