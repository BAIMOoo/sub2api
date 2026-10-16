package repository

import (
	"context"
	"database/sql"
	"errors"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/timezone"
	"github.com/Wei-Shaw/sub2api/internal/service"
)

// NewMigrationUsageSettlementRepository is deliberately separate from normal
// billing wiring. Before enabling it, every Redis->DB platform quota flusher
// must be drained/stopped and all billing caches must be made derived from DB.
// Overlapping it with the legacy flusher can overwrite committed platform usage.
func NewMigrationUsageSettlementRepository(db *sql.DB) service.MigrationUsageSettlement {
	return &usageBillingRepository{db: db}
}

// ApplyMigrationUsage commits the dedup claim, monetary effects, platform usage
// and usage log together. On unknown COMMIT outcome callers retain the event;
// replay checks the fingerprint in the same transaction before any effects.
// Window effects use the recorded original settlement time, and never move a
// newer window backwards when an old event is recovered after a reset.
func (r *usageBillingRepository) ApplyMigrationUsage(ctx context.Context, event *service.MigrationUsageEvent) (*service.UsageBillingApplyResult, error) {
	if err := event.Validate(); err != nil {
		return nil, err
	}
	if r == nil || r.db == nil {
		return nil, errors.New("migration usage repository db is nil")
	}
	tx, err := r.db.BeginTx(ctx, nil)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	cmd := event.Command
	applied, err := r.claimUsageBillingKey(ctx, tx, &cmd)
	if err != nil {
		return nil, err
	}
	if !applied {
		// This fingerprint is specific to the complete immutable event. A
		// matching committed claim proves the usage log/effects committed too.
		return &service.UsageBillingApplyResult{Applied: false}, nil
	}
	result := &service.UsageBillingApplyResult{Applied: true}
	if err := applyMigrationUsageBillingEffects(ctx, tx, event, result); err != nil {
		return nil, err
	}
	if event.PlatformCost > 0 {
		if err := incrementMigrationPlatformQuota(ctx, tx, event); err != nil {
			return nil, err
		}
	}
	usage := event.Usage
	if err := execUsageLogInsertNoResult(ctx, migrationUsageLogInserter{tx}, prepareUsageLogInsert(&usage)); err != nil {
		return nil, err
	}
	if err := tx.Commit(); err != nil {
		return nil, err
	}
	return result, nil
}

type migrationUsageLogInserter struct{ *sql.Tx }

func (q migrationUsageLogInserter) ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error) {
	result, err := q.Tx.ExecContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return nil, err
	}
	if affected != 1 {
		// A new complete-event claim must insert a new usage log. A preexisting
		// legacy log must never silently replace this event's audit record.
		return nil, service.ErrUsageBillingRequestConflict
	}
	return result, nil
}

func incrementMigrationPlatformQuota(ctx context.Context, tx *sql.Tx, event *service.MigrationUsageEvent) error {
	var daily, weekly, monthly float64
	var dailyStart, weeklyStart, monthlyStart sql.NullTime
	err := tx.QueryRowContext(ctx, `
		SELECT daily_usage_usd, weekly_usage_usd, monthly_usage_usd,
		       daily_window_start, weekly_window_start, monthly_window_start
		FROM user_platform_quotas
		WHERE user_id = $1 AND platform = $2 AND deleted_at IS NULL
		  AND (daily_limit_usd IS NOT NULL OR weekly_limit_usd IS NOT NULL OR monthly_limit_usd IS NOT NULL)
		FOR UPDATE
	`, event.Command.UserID, event.Platform).Scan(&daily, &weekly, &monthly, &dailyStart, &weeklyStart, &monthlyStart)
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	now := event.Usage.CreatedAt
	ptr := func(value sql.NullTime) *time.Time {
		if !value.Valid {
			return nil
		}
		return &value.Time
	}
	daily, newDayStart := migrationQuotaWindowEffect(daily, ptr(dailyStart), timezone.StartOfDay(now), now, event.PlatformCost)
	weekly, newWeekStart := migrationQuotaWindowEffect(weekly, ptr(weeklyStart), timezone.StartOfWeek(now), now, event.PlatformCost)
	monthStart := now
	if monthlyStart.Valid && monthlyStart.Time.After(now) {
		monthStart = monthlyStart.Time
	} else {
		monthly, monthStart = monthlyMaybeReset(monthly, ptr(monthlyStart), event.PlatformCost, now)
	}
	_, err = tx.ExecContext(ctx, `
		UPDATE user_platform_quotas
		SET daily_usage_usd = $1, weekly_usage_usd = $2, monthly_usage_usd = $3,
		    daily_window_start = $4, weekly_window_start = $5, monthly_window_start = $6,
		    updated_at = NOW()
		WHERE user_id = $7 AND platform = $8 AND deleted_at IS NULL
	`, daily, weekly, monthly, newDayStart, newWeekStart, monthStart, event.Command.UserID, event.Platform)
	return err
}

func migrationQuotaWindowEffect(usage float64, start *time.Time, target, effectiveAt time.Time, cost float64) (float64, time.Time) {
	if start != nil && start.After(effectiveAt) {
		return usage, *start
	}
	return maybeReset(usage, start, target, cost), target
}

func applyMigrationUsageBillingEffects(ctx context.Context, tx *sql.Tx, event *service.MigrationUsageEvent, result *service.UsageBillingApplyResult) error {
	cmd, effectiveAt := &event.Command, event.Usage.CreatedAt
	if cmd.SubscriptionCost > 0 && cmd.SubscriptionID != nil {
		res, err := tx.ExecContext(ctx, `
			UPDATE user_subscriptions us
			SET daily_usage_usd = us.daily_usage_usd + CASE WHEN daily_window_start > $3 THEN 0 ELSE $1 END,
			    weekly_usage_usd = us.weekly_usage_usd + CASE WHEN weekly_window_start > $3 THEN 0 ELSE $1 END,
			    monthly_usage_usd = us.monthly_usage_usd + CASE WHEN monthly_window_start > $3 THEN 0 ELSE $1 END,
			    updated_at = NOW()
			FROM groups g
			WHERE us.id = $2 AND us.deleted_at IS NULL AND us.group_id = g.id AND g.deleted_at IS NULL
		`, cmd.SubscriptionCost, *cmd.SubscriptionID, effectiveAt)
		if err != nil {
			return err
		}
		count, err := res.RowsAffected()
		if err != nil {
			return err
		}
		if count == 0 {
			return service.ErrSubscriptionNotFound
		}
	}
	if cmd.BalanceCost > 0 {
		balance, sufficient, err := deductUsageBillingBalance(ctx, tx, cmd.UserID, cmd.BalanceCost)
		if err != nil {
			return err
		}
		result.NewBalance, result.BalanceOverdrafted = &balance, !sufficient
	}
	if cmd.APIKeyQuotaCost > 0 {
		exhausted, err := incrementUsageBillingAPIKeyQuota(ctx, tx, cmd.APIKeyID, cmd.APIKeyQuotaCost)
		if err != nil && !errors.Is(err, service.ErrAPIKeyNotFound) {
			return err
		}
		result.APIKeyQuotaExhausted = exhausted
	}
	if cmd.APIKeyRateLimitCost > 0 {
		if _, err := tx.ExecContext(ctx, migrationAPIKeyWindowSQL, cmd.APIKeyRateLimitCost, cmd.APIKeyID, effectiveAt); err != nil {
			return err
		}
	}
	if cmd.AccountQuotaCost > 0 && (strings.EqualFold(cmd.AccountType, service.AccountTypeAPIKey) || strings.EqualFold(cmd.AccountType, service.AccountTypeBedrock)) {
		var state service.AccountQuotaState
		err := tx.QueryRowContext(ctx, migrationAccountQuotaSQL(), cmd.AccountQuotaCost, cmd.AccountID, effectiveAt).Scan(
			&state.TotalUsed, &state.TotalLimit, &state.DailyUsed, &state.DailyLimit, &state.WeeklyUsed, &state.WeeklyLimit,
		)
		if errors.Is(err, sql.ErrNoRows) {
			return service.ErrAccountNotFound
		}
		if err != nil {
			return err
		}
		crossed := func(used, limit float64) bool {
			return limit > 0 && used >= limit && used-cmd.AccountQuotaCost < limit
		}
		if crossed(state.TotalUsed, state.TotalLimit) || crossed(state.DailyUsed, state.DailyLimit) || crossed(state.WeeklyUsed, state.WeeklyLimit) {
			if err := enqueueSchedulerOutbox(ctx, tx, service.SchedulerOutboxEventAccountChanged, &cmd.AccountID, nil, nil); err != nil {
				return err
			}
		}
		result.QuotaState = &state
	}
	return nil
}

const migrationAPIKeyWindowSQL = `
	UPDATE api_keys SET
		usage_5h = CASE WHEN window_5h_start > $3 THEN usage_5h WHEN window_5h_start + INTERVAL '5 hours' <= $3 THEN $1 ELSE usage_5h + $1 END,
		usage_1d = CASE WHEN window_1d_start > $3 THEN usage_1d WHEN window_1d_start + INTERVAL '24 hours' <= $3 THEN $1 ELSE usage_1d + $1 END,
		usage_7d = CASE WHEN window_7d_start > $3 THEN usage_7d WHEN window_7d_start + INTERVAL '7 days' <= $3 THEN $1 ELSE usage_7d + $1 END,
		window_5h_start = CASE WHEN window_5h_start IS NULL OR window_5h_start + INTERVAL '5 hours' <= $3 THEN $3 ELSE window_5h_start END,
		window_1d_start = CASE WHEN window_1d_start IS NULL OR window_1d_start + INTERVAL '24 hours' <= $3 THEN date_trunc('day', $3::timestamptz) ELSE window_1d_start END,
		window_7d_start = CASE WHEN window_7d_start IS NULL OR window_7d_start + INTERVAL '7 days' <= $3 THEN date_trunc('day', $3::timestamptz) ELSE window_7d_start END,
		updated_at = NOW()
	WHERE id = $2 AND deleted_at IS NULL
`

func migrationAccountQuotaSQL() string {
	// The expressions are static repository constants, not user-provided SQL.
	// Replace their clock with the immutable original settlement timestamp.
	at := func(expression string) string { return strings.ReplaceAll(expression, "NOW()", "$3::timestamptz") }
	return `UPDATE accounts SET extra = (
		COALESCE(extra, '{}'::jsonb)
		|| jsonb_build_object('quota_used', COALESCE((extra->>'quota_used')::numeric, 0) + $1)
		|| CASE WHEN COALESCE((extra->>'quota_daily_limit')::numeric, 0) > 0
		    AND COALESCE((extra->>'quota_daily_start')::timestamptz, '-infinity'::timestamptz) <= $3
		THEN jsonb_build_object(
			'quota_daily_used', CASE WHEN ` + at(dailyExpiredExpr) + ` THEN $1 ELSE COALESCE((extra->>'quota_daily_used')::numeric, 0) + $1 END,
			'quota_daily_start', CASE WHEN ` + at(dailyExpiredExpr) + ` THEN ` + at(nowUTC) + ` ELSE COALESCE(extra->>'quota_daily_start', ` + at(nowUTC) + `) END)
			|| CASE WHEN ` + at(dailyExpiredExpr) + ` AND ` + at(nextDailyResetAtExpr) + ` IS NOT NULL
			   THEN jsonb_build_object('quota_daily_reset_at', ` + at(nextDailyResetAtExpr) + `) ELSE '{}'::jsonb END
		ELSE '{}'::jsonb END
		|| CASE WHEN COALESCE((extra->>'quota_weekly_limit')::numeric, 0) > 0
		    AND COALESCE((extra->>'quota_weekly_start')::timestamptz, '-infinity'::timestamptz) <= $3
		THEN jsonb_build_object(
			'quota_weekly_used', CASE WHEN ` + at(weeklyExpiredExpr) + ` THEN $1 ELSE COALESCE((extra->>'quota_weekly_used')::numeric, 0) + $1 END,
			'quota_weekly_start', CASE WHEN ` + at(weeklyExpiredExpr) + ` THEN ` + at(nowUTC) + ` ELSE COALESCE(extra->>'quota_weekly_start', ` + at(nowUTC) + `) END)
			|| CASE WHEN ` + at(weeklyExpiredExpr) + ` AND ` + at(nextWeeklyResetAtExpr) + ` IS NOT NULL
			   THEN jsonb_build_object('quota_weekly_reset_at', ` + at(nextWeeklyResetAtExpr) + `) ELSE '{}'::jsonb END
		ELSE '{}'::jsonb END
	), updated_at = NOW()
	WHERE id = $2 AND deleted_at IS NULL
	RETURNING COALESCE((extra->>'quota_used')::numeric, 0), COALESCE((extra->>'quota_limit')::numeric, 0),
	          COALESCE((extra->>'quota_daily_used')::numeric, 0), COALESCE((extra->>'quota_daily_limit')::numeric, 0),
	          COALESCE((extra->>'quota_weekly_used')::numeric, 0), COALESCE((extra->>'quota_weekly_limit')::numeric, 0)`
}
