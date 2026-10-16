//go:build unit

package repository

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/stretchr/testify/require"

	"github.com/Wei-Shaw/sub2api/internal/pkg/timezone"
	"github.com/Wei-Shaw/sub2api/internal/service"
)

func migrationUsageRepositoryEvent(t *testing.T, allEffects bool) *service.MigrationUsageEvent {
	t.Helper()
	at := time.Date(2026, 10, 16, 1, 2, 3, 0, time.UTC)
	cmd := service.UsageBillingCommand{RequestID: "migration-test", APIKeyID: 2, UserID: 1, AccountID: 3, AccountType: service.AccountTypeAPIKey, Model: "model", BalanceCost: .25}
	platform, platformCost := "", 0.0
	if allEffects {
		cmd.APIKeyQuotaCost, cmd.APIKeyRateLimitCost, cmd.AccountQuotaCost = .25, .25, .125
		platform, platformCost = "openai", .25
	}
	event, err := service.NewMigrationUsageEvent(cmd, service.UsageLog{
		RequestID: cmd.RequestID, UserID: 1, APIKeyID: 2, AccountID: 3, Model: "model",
		TotalCost: .125, ActualCost: .25, RateMultiplier: 2, CreatedAt: at,
	}, service.MigrationUsagePricingSnapshot{
		PricingAt: at, BillingModel: "model", RateMultiplier: 2, AccountRateMultiplier: 1,
		Cost:            service.CostBreakdown{TotalCost: .125, ActualCost: .25},
		ResolvedPricing: json.RawMessage(`{"input_per_million":1}`),
	}, platform, platformCost)
	require.NoError(t, err)
	return event
}

func expectMigrationUsageClaim(mock sqlmock.Sqlmock, event *service.MigrationUsageEvent) {
	mock.ExpectBegin()
	mock.ExpectQuery("INSERT INTO usage_billing_dedup").
		WithArgs(event.Command.RequestID, event.Command.APIKeyID, event.Command.RequestFingerprint).
		WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(1))
	mock.ExpectQuery("SELECT request_fingerprint.*usage_billing_dedup_archive").
		WithArgs(event.Command.RequestID, event.Command.APIKeyID).
		WillReturnError(sql.ErrNoRows)
}

func expectMigrationUsageBalance(mock sqlmock.Sqlmock) {
	mock.ExpectQuery(conditionalBalanceDeductSQL).WithArgs(.25, int64(1)).
		WillReturnRows(sqlmock.NewRows([]string{"balance"}).AddRow(99.75))
}

func TestMigrationUsageRepositoryAtomicallySettlesEveryAuthoritativeEffect(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	event := migrationUsageRepositoryEvent(t, true)
	expectMigrationUsageClaim(mock, event)
	expectMigrationUsageBalance(mock)
	mock.ExpectQuery(apiKeyQuotaIncrementSQL).WithArgs(.25, int64(2), service.StatusAPIKeyActive, service.StatusAPIKeyQuotaExhausted).
		WillReturnRows(sqlmock.NewRows([]string{"exhausted"}).AddRow(false))
	mock.ExpectExec("UPDATE api_keys SET").WithArgs(.25, int64(2), event.Usage.CreatedAt).
		WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectQuery("UPDATE accounts SET extra").WithArgs(.125, int64(3), event.Usage.CreatedAt).
		WillReturnRows(sqlmock.NewRows([]string{"total", "limit", "daily", "daily_limit", "weekly", "weekly_limit"}).AddRow(.125, 100, .125, 10, .125, 20))
	mock.ExpectQuery("SELECT daily_usage_usd.*FROM user_platform_quotas").WithArgs(int64(1), "openai").
		WillReturnRows(sqlmock.NewRows([]string{"daily", "weekly", "monthly", "day_start", "week_start", "month_start"}).AddRow(1, 2, 3, nil, nil, nil))
	mock.ExpectExec("UPDATE user_platform_quotas").WithArgs(.25, .25, .25,
		timezone.StartOfDay(event.Usage.CreatedAt), timezone.StartOfWeek(event.Usage.CreatedAt), event.Usage.CreatedAt, int64(1), "openai").
		WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec("INSERT INTO usage_logs").WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectCommit()
	result, err := NewMigrationUsageSettlementRepository(db).ApplyMigrationUsage(context.Background(), event)
	require.NoError(t, err)
	require.True(t, result.Applied)
	require.NotNil(t, result.QuotaState)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestMigrationUsageRepositoryUnknownCommitRechecksDedupWithoutRepeatingEffects(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	event := migrationUsageRepositoryEvent(t, false)
	expectMigrationUsageClaim(mock, event)
	expectMigrationUsageBalance(mock)
	mock.ExpectExec("INSERT INTO usage_logs").WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectCommit().WillReturnError(errors.New("connection lost during COMMIT"))
	repo := NewMigrationUsageSettlementRepository(db)
	_, err = repo.ApplyMigrationUsage(context.Background(), event)
	require.ErrorContains(t, err, "COMMIT")
	mock.ExpectBegin()
	mock.ExpectQuery("INSERT INTO usage_billing_dedup").
		WithArgs(event.Command.RequestID, event.Command.APIKeyID, event.Command.RequestFingerprint).
		WillReturnError(sql.ErrNoRows)
	mock.ExpectQuery("SELECT request_fingerprint.*FROM usage_billing_dedup").
		WithArgs(event.Command.RequestID, event.Command.APIKeyID).
		WillReturnRows(sqlmock.NewRows([]string{"request_fingerprint"}).AddRow(event.Command.RequestFingerprint))
	mock.ExpectRollback()
	result, err := repo.ApplyMigrationUsage(context.Background(), event)
	require.NoError(t, err)
	require.False(t, result.Applied)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestMigrationUsageRepositoryUsageLogFailureRollsBackClaimAndMoney(t *testing.T) {
	for _, mode := range []string{"insert-error", "existing-log-conflict"} {
		t.Run(mode, func(t *testing.T) {
			db, mock, err := sqlmock.New()
			require.NoError(t, err)
			defer db.Close()
			event := migrationUsageRepositoryEvent(t, false)
			expectMigrationUsageClaim(mock, event)
			expectMigrationUsageBalance(mock)
			if mode == "insert-error" {
				mock.ExpectExec("INSERT INTO usage_logs").WillReturnError(errors.New("disk full"))
			} else {
				mock.ExpectExec("INSERT INTO usage_logs").WillReturnResult(sqlmock.NewResult(0, 0))
			}
			mock.ExpectRollback()
			_, err = NewMigrationUsageSettlementRepository(db).ApplyMigrationUsage(context.Background(), event)
			require.Error(t, err)
			require.NoError(t, mock.ExpectationsWereMet())
		})
	}
}

func TestMigrationUsageRepositorySubscriptionUsesOriginalSettlementTime(t *testing.T) {
	db, mock, err := sqlmock.New()
	require.NoError(t, err)
	defer db.Close()
	base := migrationUsageRepositoryEvent(t, false)
	cmd := base.Command
	subID := int64(4)
	cmd.SubscriptionID, cmd.SubscriptionCost, cmd.BalanceCost = &subID, .25, 0
	base.Usage.SubscriptionID = &subID
	event, err := service.NewMigrationUsageEvent(cmd, base.Usage, base.Pricing, "", 0)
	require.NoError(t, err)
	expectMigrationUsageClaim(mock, event)
	mock.ExpectExec("UPDATE user_subscriptions us").WithArgs(.25, subID, event.Usage.CreatedAt).WillReturnResult(sqlmock.NewResult(0, 1))
	mock.ExpectExec("INSERT INTO usage_logs").WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectCommit()
	_, err = NewMigrationUsageSettlementRepository(db).ApplyMigrationUsage(context.Background(), event)
	require.NoError(t, err)
	require.NoError(t, mock.ExpectationsWereMet())
}

func TestMigrationUsageWindowEffectDoesNotRewindNewerWindow(t *testing.T) {
	effectiveAt := time.Date(2026, 10, 15, 12, 0, 0, 0, time.UTC)
	newer := effectiveAt.Add(24 * time.Hour)
	usage, start := migrationQuotaWindowEffect(5, &newer, timezone.StartOfDay(effectiveAt), effectiveAt, .25)
	require.Equal(t, 5.0, usage)
	require.Equal(t, newer, start)
	older := timezone.StartOfDay(effectiveAt.Add(-24 * time.Hour))
	usage, start = migrationQuotaWindowEffect(5, &older, timezone.StartOfDay(effectiveAt), effectiveAt, .25)
	require.Equal(t, .25, usage)
	require.Equal(t, timezone.StartOfDay(effectiveAt), start)
}

func TestMigrationUsageAccountWindowSQLUsesFrozenClock(t *testing.T) {
	query := migrationAccountQuotaSQL()
	require.Equal(t, 1, strings.Count(query, "NOW()"), "only updated_at uses the real DB clock")
	require.Contains(t, query, "updated_at = NOW()")
	require.Contains(t, query, "quota_daily_start')::timestamptz, '-infinity'::timestamptz) <= $3")
	require.Contains(t, query, "$3::timestamptz AT TIME ZONE")
}
