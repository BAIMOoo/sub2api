package service

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func migrationPricingFixture(t *testing.T) (*OpenAIGatewayService, *MigrationUsageJournal, context.Context, *MigrationUsageCapture) {
	t.Helper()
	journal, err := OpenMigrationUsageJournal(t.TempDir())
	require.NoError(t, err)
	t.Cleanup(func() { require.NoError(t, journal.Close()) })
	svc := newOpenAIRecordUsageServiceForTest(&openAIRecordUsageLogRepoStub{}, &openAIRecordUsageUserRepoStub{}, nil, nil)
	svc.resolver = NewModelPricingResolver(nil, svc.billingService)
	ctx, capture := WithMigrationUsageCapture(context.Background(), journal, MigrationUsageCaptureOptions{})
	return svc, journal, ctx, capture
}

func migrationPricingInput(id, model string) *OpenAIRecordUsageInput {
	groupID := int64(5)
	return &OpenAIRecordUsageInput{
		Result: &OpenAIForwardResult{RequestID: id, Model: model, Usage: OpenAIUsage{InputTokens: 200, OutputTokens: 30}},
		APIKey: &APIKey{ID: 2, Key: "never-journal-user-api-key", GroupID: &groupID,
			Group: &Group{ID: groupID, Platform: PlatformOpenAI, RateMultiplier: 0.8, Hydrated: true, LongContextPricingEnabled: true}},
		User: &User{ID: 1}, Account: &Account{ID: 3, Platform: PlatformOpenAI, Type: AccountTypeAPIKey,
			Credentials: map[string]any{"api_key": "never-journal-upstream-api-key"}},
		PricingAt: time.Date(2026, 8, 24, 1, 59, 59, 0, time.UTC),
	}
}

func migrationReadPricing(t *testing.T, journal *MigrationUsageJournal, capture *MigrationUsageCapture) (*MigrationUsageEvent, migrationTokenPricingDecision) {
	t.Helper()
	require.NoError(t, capture.CheckPersisted())
	pending, err := journal.Pending()
	require.NoError(t, err)
	require.Len(t, pending, 1)
	var decision migrationTokenPricingDecision
	require.NoError(t, json.Unmarshal(pending[0].Pricing.ResolvedPricing, &decision))
	require.Equal(t, migrationCanonicalTokenCost(pending[0].Pricing.Cost), decision.Cost)
	encoded, err := json.Marshal(pending[0])
	require.NoError(t, err)
	require.NotContains(t, string(encoded), "never-journal-")
	return pending[0], decision
}

func TestMigrationPricingDefaultUsesActualCalculationAndIgnoresHandlerGuesses(t *testing.T) {
	svc, journal, _, _ := migrationPricingFixture(t)
	ctx, capture := WithMigrationUsageCapture(context.Background(), journal, MigrationUsageCaptureOptions{
		BillingModel: "forged-handler-model", PricingAt: time.Date(2000, 1, 1, 0, 0, 0, 0, time.UTC),
		ResolvedPricing: json.RawMessage(`{"api_key":"never-journal-handler-guess"}`),
	})
	input := migrationPricingInput("default-price", "gpt-5.4-nano")
	require.NoError(t, svc.RecordUsage(ctx, input))
	event, decision := migrationReadPricing(t, journal, capture)
	require.Equal(t, input.Result.Model, event.Pricing.BillingModel)
	require.Equal(t, input.PricingAt, event.Pricing.PricingAt)
	require.Equal(t, PricingSourceLiteLLM, decision.Source)
	require.Positive(t, decision.UnitPricing.InputPricePerToken)
	require.Equal(t, 0.8, decision.RateMultiplier)
	require.Equal(t, input.Result.Usage.InputTokens, decision.Tokens.InputTokens)
	require.Equal(t, input.Result.Usage.OutputTokens, decision.Tokens.OutputTokens)
}

func TestMigrationPricingChannelFreezesSelectedIntervalAndExactRules(t *testing.T) {
	svc, journal, ctx, capture := migrationPricingFixture(t)
	input := migrationPricingInput("channel-price", "gpt-5.4-nano")
	groupID := *input.APIKey.GroupID
	inputPrice, outputPrice, fast := 1e-6, 2e-6, 2.0
	intervalInput, intervalOutput := 9e-6, 13e-6
	limit := 100
	channelPrice := &ChannelModelPricing{
		ID: 42, ChannelID: 7, Platform: PlatformOpenAI, Models: []string{input.Result.Model}, BillingMode: BillingModeToken,
		InputPrice: &inputPrice, OutputPrice: &outputPrice, FastMultiplier: &fast,
		ReasoningEffortMultipliers: map[string]float64{"high": 1.5},
		Intervals: []PricingInterval{
			{MinTokens: 0, MaxTokens: &limit, TierLabel: "small"},
			{MinTokens: 101, TierLabel: "selected-large", InputPrice: &intervalInput, OutputPrice: &intervalOutput},
		},
		TimePricing: &ChannelTimePricing{Timezone: "UTC", Periods: []ChannelTimePricingPeriod{{StartTime: "00:00", EndTime: "02:00", Multiplier: 3}}},
	}
	cache := newEmptyChannelCache()
	cache.pricingByGroupModel[channelModelKey{groupID: groupID, platform: PlatformOpenAI, model: input.Result.Model}] = channelPrice
	cache.channelByGroupID[groupID] = &Channel{ID: groupID, Status: StatusActive}
	cache.groupPlatform[groupID] = PlatformOpenAI
	cache.loadedAt = time.Now()
	channelService := &ChannelService{}
	channelService.cache.Store(cache)
	svc.resolver = NewModelPricingResolver(channelService, svc.billingService)
	tier, effort := "priority", "high"
	input.Result.ServiceTier, input.Result.ReasoningEffort = &tier, &effort
	require.NoError(t, svc.RecordUsage(ctx, input))
	// These operator-owned objects may change before journal replay. The
	// already-observed decision must own every pointer/map it serializes.
	intervalInput = 99
	channelPrice.TimePricing.Periods[0].Multiplier = 99
	channelPrice.ReasoningEffortMultipliers["high"] = 99
	event, decision := migrationReadPricing(t, journal, capture)
	require.Equal(t, PricingSourceChannel, decision.Source)
	require.NotNil(t, decision.SelectedInterval)
	require.Equal(t, "selected-large", decision.SelectedInterval.TierLabel)
	require.Equal(t, 9e-6, decision.UnitPricing.InputPricePerToken)
	require.Equal(t, 13e-6, decision.UnitPricing.OutputPricePerToken)
	require.Equal(t, 1.5, decision.UnitPricing.ReasoningEffortMultipliers["high"])
	require.Equal(t, 3.0, decision.ChannelTimeMultiplier)
	require.Equal(t, 3.0, decision.ChannelTimeRules.Periods[0].Multiplier)
	require.Equal(t, 1.5, decision.ReasoningEffortMultiplier)
	require.Equal(t, 2.0, *decision.UnitPricing.FastMultiplier)
	require.False(t, decision.ApplyLongContext, "explicit intervals already implement context tiers")
	wantTotal := (200*9e-6 + 30*13e-6) * 2 * 3 * 1.5
	require.InDelta(t, wantTotal, event.Pricing.Cost.TotalCost, 1e-12)
	require.InDelta(t, wantTotal*0.8, event.Pricing.Cost.ActualCost, 1e-12)
	// It is currently many weeks past 02:00 UTC on PricingAt's day. The
	// decision still uses the request's 01:59:59 schedule and selected prices.
	require.Equal(t, input.PricingAt, event.Pricing.PricingAt)
}

func TestMigrationPricingResponseModelBindsChosenCandidate(t *testing.T) {
	for _, test := range []struct{ name, baseline, response, selected string }{
		{"adopt-cheaper", "gpt-5.5", "gpt-5.4-nano", "gpt-5.4-nano"},
		{"reject-pricier", "gpt-5.4-nano", "gpt-5.5", "gpt-5.4-nano"},
	} {
		t.Run(test.name, func(t *testing.T) {
			svc, journal, ctx, capture := migrationPricingFixture(t)
			input := migrationPricingInput(test.name, test.baseline)
			input.Result.UpstreamResponseModel = test.response
			input.BillingModelSource = BillingModelSourceResponse
			require.NoError(t, svc.RecordUsage(ctx, input))
			event, decision := migrationReadPricing(t, journal, capture)
			require.Equal(t, test.selected, event.Pricing.BillingModel)
			require.Equal(t, test.selected, decision.Model)
			require.Equal(t, test.baseline, event.Usage.Model, "business usage attribution is preserved")
			pricing, err := svc.billingService.GetModelPricing(test.selected)
			require.NoError(t, err)
			require.Equal(t, pricing.InputPricePerToken, decision.UnitPricing.InputPricePerToken)
		})
	}
}

func TestMigrationPricingRecordsActualFallbackCandidate(t *testing.T) {
	svc, journal, ctx, capture := migrationPricingFixture(t)
	input := migrationPricingInput("fallback-candidate", "gpt-5.4-nano")
	input.Result.BillingModel = "unpriced-migration-fixture"
	require.NoError(t, svc.RecordUsage(ctx, input))
	event, decision := migrationReadPricing(t, journal, capture)
	require.Equal(t, "gpt-5.4-nano", event.Pricing.BillingModel)
	require.Equal(t, "gpt-5.4-nano", decision.Model)
}

func TestMigrationPricingFreeFastRetainsPriorityAndStandardCalculations(t *testing.T) {
	svc, journal, ctx, capture := migrationPricingFixture(t)
	input := migrationPricingInput("free-fast", "gpt-5.6-sol")
	input.Account.Type = AccountTypeOAuth
	input.APIKey.Group.FreeOpenAIFast = true
	input.APIKey.Group.RateMultiplier = 0.5
	unitInput, unitOutput, fast := 0.001, 0.002, 3.0
	input.APIKey.Group.ModelPricing = []ChannelModelPricing{{Models: []string{input.Result.Model}, BillingMode: BillingModeToken,
		InputPrice: &unitInput, OutputPrice: &unitOutput, FastMultiplier: &fast}}
	tier := "priority"
	input.Result.ServiceTier = &tier
	input.Result.UpstreamResponseServiceTier = "default"
	require.NoError(t, svc.RecordUsage(ctx, input))
	event, decision := migrationReadPricing(t, journal, capture)
	require.Equal(t, "priority", decision.ServiceTier)
	require.NotNil(t, decision.CustomerChargePricing)
	require.Empty(t, decision.CustomerChargePricing.ServiceTier)
	require.Equal(t, input.Result.Model, decision.CustomerChargePricing.Model)
	standardTotal := 200*unitInput + 30*unitOutput
	require.InDelta(t, standardTotal*fast, event.Pricing.Cost.TotalCost, 1e-12)
	require.InDelta(t, standardTotal*0.5, event.Pricing.Cost.ActualCost, 1e-12)
	require.Equal(t, decision.CustomerChargePricing.Cost.ActualCost, event.Pricing.Cost.ActualCost)
}

func TestMigrationPricingDeepSeekPeakBoundaryUsesOriginalRequestDecision(t *testing.T) {
	for _, test := range []struct {
		name       string
		at         time.Time
		multiplier float64
	}{
		{"before-boundary", time.Date(2026, 8, 24, 3, 59, 59, 0, time.UTC), 2},
		{"at-boundary", time.Date(2026, 8, 24, 4, 0, 0, 0, time.UTC), 1},
	} {
		t.Run(test.name, func(t *testing.T) {
			svc, journal, ctx, capture := migrationPricingFixture(t)
			input := migrationPricingInput(test.name, "deepseek-v4-pro")
			input.Account.Platform, input.APIKey.Group.Platform = PlatformDeepseek, PlatformDeepseek
			input.PricingAt = test.at
			require.NoError(t, svc.RecordUsage(ctx, input))
			event, decision := migrationReadPricing(t, journal, capture)
			require.Equal(t, test.at, event.Pricing.PricingAt)
			require.Equal(t, test.multiplier, decision.DeepSeekPeakMultiplier)
			require.InDelta(t, deepseekProOffPeakInputPrice*test.multiplier, decision.UnitPricing.InputPricePerToken, 1e-20)
			require.InDelta(t, deepseekProOffPeakOutputPrice*test.multiplier, decision.UnitPricing.OutputPricePerToken, 1e-20)
			// Current catalog policy bills this former Pro model as Flash. The
			// pending immutable event must retain its original pre-switch Pro rate.
			current, err := svc.billingService.GetModelPricing(input.Result.Model)
			require.NoError(t, err)
			require.NotEqual(t, current.InputPricePerToken, decision.UnitPricing.InputPricePerToken)
		})
	}
}

func TestMigrationPricingSearchSurchargeIsExplicitlyBound(t *testing.T) {
	svc, journal, ctx, capture := migrationPricingFixture(t)
	input := migrationPricingInput("search-surcharge", "gpt-5.4-nano")
	input.Result.SearchCount = 3
	require.NoError(t, svc.RecordUsage(ctx, input))
	event, decision := migrationReadPricing(t, journal, capture)
	require.NotNil(t, decision.SearchSurcharge)
	require.Equal(t, 3, decision.SearchSurcharge.Calls)
	require.Equal(t, defaultSearchPricePer1k, decision.SearchSurcharge.PricePer1K)
	require.InDelta(t, 3*defaultSearchPricePer1k/1000, decision.SearchSurcharge.Cost.TotalCost, 1e-12)
	require.Greater(t, event.Pricing.Cost.TotalCost, decision.SearchSurcharge.Cost.TotalCost)
}

func TestMigrationPricingMissingOrModifiedCalculationFailsClosed(t *testing.T) {
	for _, modified := range []bool{false, true} {
		t.Run(map[bool]string{false: "unobserved", true: "modified"}[modified], func(t *testing.T) {
			svc, journal, ctx, capture := migrationPricingFixture(t)
			event := migrationUsageTestEvent(t, "incomplete-price")
			cost := &event.Pricing.Cost
			if modified {
				var err error
				cost, err = svc.billingService.CalculateTokenCostForRequest(TokenCostRequest{
					Ctx: ctx, Model: "gpt-5.4-nano", Tokens: UsageTokens{InputTokens: 100}, RateMultiplier: 1,
				})
				require.NoError(t, err)
				cost.ActualCost += 1
			}
			_, err := applyUsageBilling(ctx, event.Command.RequestID, &event.Usage, &postUsageBillingParams{
				Cost: cost, User: &User{ID: 1}, APIKey: &APIKey{ID: 2}, Account: &Account{ID: 3},
			}, &billingDeps{}, nil)
			require.ErrorIs(t, err, ErrMigrationUsageCaptureIncomplete)
			require.ErrorIs(t, capture.CheckPersisted(), ErrMigrationUsageCaptureIncomplete)
			pending, err := journal.Pending()
			require.NoError(t, err)
			require.Empty(t, pending)
		})
	}
}
