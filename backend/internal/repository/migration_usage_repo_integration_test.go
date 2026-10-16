//go:build integration

package repository

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stretchr/testify/require"

	"github.com/Wei-Shaw/sub2api/internal/service"
)

func newMigrationUsageIntegrationEvent(t *testing.T, userID, keyID, accountID int64, at time.Time, platform bool) *service.MigrationUsageEvent {
	t.Helper()
	requestID := uuid.NewString()
	platformName, platformCost := "", 0.0
	if platform {
		platformName, platformCost = "openai", .25
	}
	event, err := service.NewMigrationUsageEvent(service.UsageBillingCommand{
		RequestID: requestID, UserID: userID, APIKeyID: keyID, AccountID: accountID,
		AccountType: service.AccountTypeAPIKey, Model: "migration-model",
		BalanceCost: .25, APIKeyQuotaCost: .25, APIKeyRateLimitCost: .25, AccountQuotaCost: .125,
	}, service.UsageLog{
		RequestID: requestID, UserID: userID, APIKeyID: keyID, AccountID: accountID,
		Model: "migration-model", TotalCost: .125, ActualCost: .25, RateMultiplier: 2, CreatedAt: at,
	}, service.MigrationUsagePricingSnapshot{
		PricingAt: at.Add(-time.Minute), BillingModel: "migration-model", RateMultiplier: 2, AccountRateMultiplier: 1,
		Cost: service.CostBreakdown{TotalCost: .125, ActualCost: .25}, ResolvedPricing: json.RawMessage(`{"request_price":0.125}`),
	}, platformName, platformCost)
	require.NoError(t, err)
	return event
}

func TestMigrationUsageIntegrationAtomicReplayAndFrozenWindows(t *testing.T) {
	ctx := context.Background()
	for _, mode := range []string{"rolling", "fixed"} {
		t.Run(mode, func(t *testing.T) {
			client := testEntClient(t)
			user := mustCreateUser(t, client, &service.User{Balance: 100})
			key := mustCreateApiKey(t, client, &service.APIKey{UserID: user.ID, Quota: 10, RateLimit5h: 10, RateLimit1d: 10, RateLimit7d: 10})
			account := mustCreateAccount(t, client, &service.Account{
				Name: "migration-" + uuid.NewString(), Type: service.AccountTypeAPIKey,
				Extra: map[string]any{"quota_limit": .1, "quota_daily_limit": 10, "quota_weekly_limit": 20,
					"quota_daily_reset_mode": mode, "quota_weekly_reset_mode": mode},
			})
			_, err := integrationDB.ExecContext(ctx, `INSERT INTO user_platform_quotas (user_id, platform, daily_limit_usd) VALUES ($1, 'openai', 10)`, user.ID)
			require.NoError(t, err)
			// Recover a settlement two days after it originally completed. Every
			// counter must use its original time, not the recovery wall clock.
			at := time.Now().UTC().Add(-48 * time.Hour).Truncate(time.Microsecond)
			event := newMigrationUsageIntegrationEvent(t, user.ID, key.ID, account.ID, at, true)
			repo := NewMigrationUsageSettlementRepository(integrationDB)
			result, err := repo.ApplyMigrationUsage(ctx, event)
			require.NoError(t, err)
			require.True(t, result.Applied)
			result, err = repo.ApplyMigrationUsage(ctx, event)
			require.NoError(t, err)
			require.False(t, result.Applied)
			var balance, quota, keyUsage, total, daily, platformUsage float64
			var keyStart, accountStart time.Time
			require.NoError(t, integrationDB.QueryRowContext(ctx, "SELECT balance FROM users WHERE id=$1", user.ID).Scan(&balance))
			require.Equal(t, 99.75, balance)
			require.NoError(t, integrationDB.QueryRowContext(ctx, "SELECT quota_used, usage_5h, window_5h_start FROM api_keys WHERE id=$1", key.ID).Scan(&quota, &keyUsage, &keyStart))
			require.Equal(t, .25, quota)
			require.Equal(t, .25, keyUsage)
			require.True(t, at.Equal(keyStart), "key window must use original settlement time")
			require.NoError(t, integrationDB.QueryRowContext(ctx, `SELECT (extra->>'quota_used')::numeric, (extra->>'quota_daily_used')::numeric, (extra->>'quota_daily_start')::timestamptz FROM accounts WHERE id=$1`, account.ID).Scan(&total, &daily, &accountStart))
			require.Equal(t, .125, total)
			require.Equal(t, .125, daily)
			require.True(t, at.Equal(accountStart))
			require.NoError(t, integrationDB.QueryRowContext(ctx, "SELECT daily_usage_usd FROM user_platform_quotas WHERE user_id=$1 AND platform='openai'", user.ID).Scan(&platformUsage))
			require.Equal(t, .25, platformUsage)
			var logCount, dedupCount, outboxCount int
			require.NoError(t, integrationDB.QueryRowContext(ctx, "SELECT count(*) FROM usage_logs WHERE request_id=$1 AND api_key_id=$2", event.Command.RequestID, key.ID).Scan(&logCount))
			require.NoError(t, integrationDB.QueryRowContext(ctx, "SELECT count(*) FROM usage_billing_dedup WHERE request_id=$1 AND api_key_id=$2", event.Command.RequestID, key.ID).Scan(&dedupCount))
			require.NoError(t, integrationDB.QueryRowContext(ctx, "SELECT count(*) FROM scheduler_outbox WHERE event_type=$1 AND account_id=$2", service.SchedulerOutboxEventAccountChanged, account.ID).Scan(&outboxCount))
			require.Equal(t, 1, logCount)
			require.Equal(t, 1, dedupCount)
			require.Equal(t, 1, outboxCount)

			// A later window already exists. Recovering another old settlement
			// still changes lifetime money, but must not consume the new window.
			newer := at.Add(24 * time.Hour)
			_, err = integrationDB.ExecContext(ctx, `UPDATE api_keys SET usage_5h=9, usage_1d=9, usage_7d=9, window_5h_start=$1, window_1d_start=$1, window_7d_start=$1 WHERE id=$2`, newer, key.ID)
			require.NoError(t, err)
			patch, err := json.Marshal(map[string]any{"quota_daily_start": newer.Format(time.RFC3339Nano), "quota_weekly_start": newer.Format(time.RFC3339Nano), "quota_daily_used": 9, "quota_weekly_used": 9})
			require.NoError(t, err)
			_, err = integrationDB.ExecContext(ctx, `UPDATE accounts SET extra=extra||$1::jsonb WHERE id=$2`, string(patch), account.ID)
			require.NoError(t, err)
			_, err = integrationDB.ExecContext(ctx, `UPDATE user_platform_quotas SET daily_usage_usd=9, weekly_usage_usd=9, monthly_usage_usd=9, daily_window_start=$1, weekly_window_start=$1, monthly_window_start=$1 WHERE user_id=$2 AND platform='openai'`, newer, user.ID)
			require.NoError(t, err)
			olderEvent := newMigrationUsageIntegrationEvent(t, user.ID, key.ID, account.ID, at.Add(time.Second), true)
			_, err = repo.ApplyMigrationUsage(ctx, olderEvent)
			require.NoError(t, err)
			require.NoError(t, integrationDB.QueryRowContext(ctx, "SELECT balance FROM users WHERE id=$1", user.ID).Scan(&balance))
			require.Equal(t, 99.5, balance)
			require.NoError(t, integrationDB.QueryRowContext(ctx, "SELECT usage_5h, window_5h_start FROM api_keys WHERE id=$1", key.ID).Scan(&keyUsage, &keyStart))
			require.Equal(t, 9.0, keyUsage)
			require.True(t, newer.Equal(keyStart))
			require.NoError(t, integrationDB.QueryRowContext(ctx, `SELECT (extra->>'quota_used')::numeric, (extra->>'quota_daily_used')::numeric FROM accounts WHERE id=$1`, account.ID).Scan(&total, &daily))
			require.Equal(t, .25, total)
			require.Equal(t, 9.0, daily)
			require.NoError(t, integrationDB.QueryRowContext(ctx, "SELECT daily_usage_usd FROM user_platform_quotas WHERE user_id=$1 AND platform='openai'", user.ID).Scan(&platformUsage))
			require.Equal(t, 9.0, platformUsage)
		})
	}
}

func TestMigrationUsageIntegrationLogFailureRollsBackMoneyAndDedup(t *testing.T) {
	ctx := context.Background()
	client := testEntClient(t)
	user := mustCreateUser(t, client, &service.User{Balance: 100})
	key := mustCreateApiKey(t, client, &service.APIKey{UserID: user.ID, Quota: 10})
	account := mustCreateAccount(t, client, &service.Account{Name: "migration-rollback-" + uuid.NewString(), Type: service.AccountTypeAPIKey})
	base := newMigrationUsageIntegrationEvent(t, user.ID, key.ID, account.ID, time.Now().UTC(), false)
	// Force a real database constraint failure in the last transaction step.
	base.Command.Model, base.Usage.Model = strings.Repeat("x", 201), strings.Repeat("x", 201)
	event, err := service.NewMigrationUsageEvent(base.Command, base.Usage, base.Pricing, "", 0)
	require.NoError(t, err)
	_, err = NewMigrationUsageSettlementRepository(integrationDB).ApplyMigrationUsage(ctx, event)
	require.Error(t, err)
	var balance, quota float64
	var count int
	require.NoError(t, integrationDB.QueryRowContext(ctx, "SELECT balance FROM users WHERE id=$1", user.ID).Scan(&balance))
	require.Equal(t, 100.0, balance)
	require.NoError(t, integrationDB.QueryRowContext(ctx, "SELECT quota_used FROM api_keys WHERE id=$1", key.ID).Scan(&quota))
	require.Zero(t, quota)
	require.NoError(t, integrationDB.QueryRowContext(ctx, "SELECT count(*) FROM usage_billing_dedup WHERE request_id=$1 AND api_key_id=$2", event.Command.RequestID, key.ID).Scan(&count))
	require.Zero(t, count)
}
