package service

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/stretchr/testify/require"

	"github.com/Wei-Shaw/sub2api/internal/config"
)

type migrationCaptureUnusedPlatformRepo struct{ UserPlatformQuotaRepository }

func migrationCaptureOptions() MigrationUsageCaptureOptions {
	return MigrationUsageCaptureOptions{
		PricingAt: time.Date(2026, 10, 16, 1, 2, 3, 0, time.UTC), BillingModel: "gpt-5.1",
		ResolvedPricing:              json.RawMessage(`{"input_per_million":1.25,"output_per_million":10}`),
		PlatformQuotaDBAuthoritative: true,
	}
}

func TestMigrationUsageCaptureOpenAIRecordUsageDoesNotApplyLegacyEffects(t *testing.T) {
	for _, platform := range []string{PlatformOpenAI, PlatformGrok, PlatformDeepseek} {
		t.Run(platform, func(t *testing.T) {
			journal, err := OpenMigrationUsageJournal(t.TempDir())
			require.NoError(t, err)
			defer journal.Close()
			usageRepo := &openAIRecordUsageLogRepoStub{inserted: true}
			billingRepo := &openAIRecordUsageBillingRepoStub{result: &UsageBillingApplyResult{Applied: true}}
			userRepo := &openAIRecordUsageUserRepoStub{}
			quota := &openAIRecordUsageAPIKeyQuotaStub{}
			svc := newOpenAIRecordUsageServiceWithBillingRepoForTest(usageRepo, billingRepo, userRepo, nil, nil)
			svc.userPlatformQuotaRepo = &migrationCaptureUnusedPlatformRepo{}
			options := migrationCaptureOptions()
			ctx, capture := WithMigrationUsageCapture(context.Background(), journal, options)
			err = svc.RecordUsage(ctx, &OpenAIRecordUsageInput{
				Result: &OpenAIForwardResult{RequestID: "captured-" + platform, Model: "gpt-5.1", Usage: OpenAIUsage{InputTokens: 100, OutputTokens: 20}},
				APIKey: &APIKey{ID: 2, Quota: 100, RateLimit5h: 10, Group: &Group{Platform: platform, RateMultiplier: 1}},
				User:   &User{ID: 1}, Account: &Account{ID: 3, Platform: platform, Type: AccountTypeAPIKey, Extra: map[string]any{"quota_limit": 100.0}},
				PricingAt: options.PricingAt, APIKeyService: quota,
			})
			require.NoError(t, err)
			require.NoError(t, capture.CheckPersisted())
			require.NotEmpty(t, capture.EventID())
			require.Zero(t, billingRepo.calls)
			require.Zero(t, userRepo.deductCalls)
			require.Zero(t, usageRepo.calls)
			require.Zero(t, quota.quotaCalls)
			require.Zero(t, quota.rateLimitCalls)
			pending, err := journal.Pending()
			require.NoError(t, err)
			require.Len(t, pending, 1)
			require.Positive(t, pending[0].Command.BalanceCost)
			require.Positive(t, pending[0].Command.APIKeyQuotaCost)
			require.Positive(t, pending[0].Command.APIKeyRateLimitCost)
			require.Positive(t, pending[0].Command.AccountQuotaCost)
			require.Equal(t, platform, pending[0].Platform)
			require.Positive(t, pending[0].PlatformCost)
		})
	}
}

func TestMigrationUsageCaptureGatewayRecordUsageDoesNotApplyLegacyEffects(t *testing.T) {
	for _, platform := range []string{PlatformAnthropic, PlatformGemini, PlatformAntigravity} {
		t.Run(platform, func(t *testing.T) {
			journal, err := OpenMigrationUsageJournal(t.TempDir())
			require.NoError(t, err)
			defer journal.Close()
			cfg := &config.Config{}
			cfg.Default.RateMultiplier = 1
			billing := NewBillingService(cfg, nil)
			usageRepo := &openAIRecordUsageLogRepoStub{inserted: true}
			userRepo := &openAIRecordUsageUserRepoStub{}
			svc := &GatewayService{
				cfg: cfg, billingService: billing, resolver: NewModelPricingResolver(nil, billing),
				usageLogRepo: usageRepo, userRepo: userRepo,
				userPlatformQuotaRepo: &migrationCaptureUnusedPlatformRepo{},
			}
			options := migrationCaptureOptions()
			options.BillingModel = "claude-sonnet-4-5"
			ctx, capture := WithMigrationUsageCapture(context.Background(), journal, options)
			err = svc.RecordUsage(ctx, &RecordUsageInput{
				Result: &ForwardResult{RequestID: "captured-" + platform, Model: "claude-sonnet-4-5", Usage: ClaudeUsage{InputTokens: 100, OutputTokens: 20}},
				APIKey: &APIKey{ID: 2, Group: &Group{Platform: platform, RateMultiplier: 1}},
				User:   &User{ID: 1}, Account: &Account{ID: 3, Platform: platform, Type: AccountTypeOAuth}, PricingAt: options.PricingAt,
			})
			require.NoError(t, err)
			require.NoError(t, capture.CheckPersisted())
			require.Zero(t, userRepo.deductCalls)
			require.Zero(t, usageRepo.calls)
			pending, err := journal.Pending()
			require.NoError(t, err)
			require.Len(t, pending, 1)
			require.Positive(t, pending[0].Command.BalanceCost)
		})
	}
}

func TestMigrationUsageCaptureRejectsSimpleUsageOnlyAndUnhandedPlatformQuota(t *testing.T) {
	journal, err := OpenMigrationUsageJournal(t.TempDir())
	require.NoError(t, err)
	defer journal.Close()
	ctx, capture := WithMigrationUsageCapture(context.Background(), journal, migrationCaptureOptions())
	writeUsageLogBestEffort(ctx, nil, &UsageLog{RequestID: "simple", APIKeyID: 2}, "test")
	require.ErrorIs(t, capture.CheckPersisted(), ErrMigrationUsageCaptureIncomplete)
	options := migrationCaptureOptions()
	options.PlatformQuotaDBAuthoritative = false
	ctx, capture = WithMigrationUsageCapture(context.Background(), journal, options)
	event := migrationUsageTestEvent(t, "legacy-platform")
	_, err = applyUsageBilling(ctx, event.Command.RequestID, &event.Usage, &postUsageBillingParams{
		Cost: &event.Pricing.Cost, User: &User{ID: 1}, APIKey: &APIKey{ID: 2}, Account: &Account{ID: 3}, Platform: "openai",
	}, &billingDeps{userPlatformQuotaRepo: &migrationCaptureUnusedPlatformRepo{}}, nil)
	require.ErrorContains(t, err, "legacy flusher handoff")
	require.Error(t, capture.CheckPersisted())
}

func TestMigrationUsageCaptureDiskErrorCannotBeMistakenForPersistence(t *testing.T) {
	journal, err := OpenMigrationUsageJournal(t.TempDir())
	require.NoError(t, err)
	defer journal.Close()
	journal.syncFile = func(*os.File) error { return errors.New("disk unavailable") }
	ctx, capture := WithMigrationUsageCapture(context.Background(), journal, migrationCaptureOptions())
	event := migrationUsageTestEvent(t, "disk-error")
	migrationObserveTokenPricing(ctx, &event.Pricing.Cost, migrationTokenPricingDecision{
		Model: event.Pricing.BillingModel, PricingAt: event.Pricing.PricingAt,
	})
	_, err = applyUsageBilling(ctx, event.Command.RequestID, &event.Usage, &postUsageBillingParams{
		Cost: &event.Pricing.Cost, User: &User{ID: 1}, APIKey: &APIKey{ID: 2}, Account: &Account{ID: 3},
	}, &billingDeps{}, nil)
	require.ErrorContains(t, err, "disk unavailable")
	writeUsageLogBestEffort(ctx, nil, &event.Usage, "test")
	require.ErrorContains(t, capture.CheckPersisted(), "disk unavailable")
	require.Empty(t, capture.EventID())
}
