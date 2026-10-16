package service

import (
	"context"
	"encoding/json"
	"fmt"
	"time"
)

// migrationTokenPricingDecision is deliberately an allowlist of pricing data.
// Never embed CostInput, TokenCostRequest, Group, APIKey or Account here: those
// objects can contain authentication credentials and unrelated mutable state.
// UnitPricing is the actual interval/policy-adjusted card consumed by the
// calculation. The recorded multipliers are results from that same calculation,
// not a later lookup against possibly changed schedules or prices.
type migrationTokenPricingDecision struct {
	Version                   int                            `json:"version"`
	Model                     string                         `json:"model"`
	PricingAt                 time.Time                      `json:"pricing_at"`
	Source                    string                         `json:"source"`
	Tokens                    UsageTokens                    `json:"tokens"`
	RateMultiplier            float64                        `json:"rate_multiplier"`
	ServiceTier               string                         `json:"service_tier,omitempty"`
	ReasoningEffort           string                         `json:"reasoning_effort,omitempty"`
	UnitPricing               ModelPricing                   `json:"unit_pricing"`
	ContextTokens             int                            `json:"context_tokens"`
	IntervalContextTokens     int                            `json:"interval_context_tokens"`
	SelectedInterval          *PricingInterval               `json:"selected_interval,omitempty"`
	ApplyLongContext          bool                           `json:"apply_long_context"`
	DeepSeekPeakMultiplier    float64                        `json:"deepseek_peak_multiplier"`
	ChannelTimeMultiplier     float64                        `json:"channel_time_multiplier"`
	ChannelTimeRules          *ChannelTimePricing            `json:"channel_time_rules,omitempty"`
	ReasoningEffortMultiplier float64                        `json:"reasoning_effort_multiplier"`
	Cost                      CostBreakdown                  `json:"cost"`
	CustomerChargePricing     *migrationTokenPricingDecision `json:"customer_charge_pricing,omitempty"`
	SearchSurcharge           *migrationSearchPricing        `json:"search_surcharge,omitempty"`
}

type migrationSearchPricing struct {
	Calls          int           `json:"calls"`
	PricePer1K     float64       `json:"price_per_1k"`
	RateMultiplier float64       `json:"rate_multiplier"`
	Cost           CostBreakdown `json:"cost"`
}

func migrationCanonicalTokenCost(cost CostBreakdown) CostBreakdown {
	if cost.BillingMode == "" {
		cost.BillingMode = string(BillingModeToken)
	}
	return cost
}

func migrationClonePricingDecision(decision *migrationTokenPricingDecision) (*migrationTokenPricingDecision, error) {
	data, err := json.Marshal(decision)
	if err != nil {
		return nil, err
	}
	var clone migrationTokenPricingDecision
	if err := json.Unmarshal(data, &clone); err != nil {
		return nil, err
	}
	return &clone, nil
}

func migrationObserveTokenPricing(ctx context.Context, cost *CostBreakdown, decision migrationTokenPricingDecision) {
	capture := migrationUsageCaptureFromContext(ctx)
	if capture == nil {
		return
	}
	capture.mu.Lock()
	defer capture.mu.Unlock()
	if capture.err != nil {
		return
	}
	if cost == nil {
		capture.err = fmt.Errorf("%w: missing calculated token cost", ErrMigrationUsageCaptureIncomplete)
		return
	}
	decision.Version = 1
	decision.Cost = migrationCanonicalTokenCost(*cost)
	clone, err := migrationClonePricingDecision(&decision)
	if err != nil {
		capture.err = fmt.Errorf("%w: freeze calculated token pricing: %v", ErrMigrationUsageCaptureIncomplete, err)
		return
	}
	if capture.pricing == nil {
		capture.pricing = make(map[*CostBreakdown]*migrationTokenPricingDecision)
	}
	capture.pricing[cost] = clone
}

// migrationObserveStandardCharge binds the actual Standard re-evaluation to
// the selected priority result. A different response-model candidate must never
// replace the selected decision just because its calculation ran last.
func migrationObserveStandardCharge(ctx context.Context, selected, standard *CostBreakdown) {
	c := migrationUsageCaptureFromContext(ctx)
	if c == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.err != nil {
		return
	}
	selectedDecision, standardDecision := c.pricing[selected], c.pricing[standard]
	if selected == nil || standard == nil || selectedDecision == nil || standardDecision == nil ||
		migrationCanonicalTokenCost(*standard) != standardDecision.Cost || selectedDecision.Model != standardDecision.Model {
		c.err = fmt.Errorf("%w: Standard customer charge has no matching observed price", ErrMigrationUsageCaptureIncomplete)
		return
	}
	expected := selectedDecision.Cost
	expected.ActualCost = standard.ActualCost
	if migrationCanonicalTokenCost(*selected) != expected {
		c.err = fmt.Errorf("%w: selected price changed outside Standard charge calculation", ErrMigrationUsageCaptureIncomplete)
		return
	}
	selectedDecision.CustomerChargePricing = standardDecision
	selectedDecision.Cost = expected
}

func migrationObserveSearchSurcharge(ctx context.Context, selected, search *CostBreakdown, calls int, pricePer1K *float64, rateMultiplier float64) {
	c := migrationUsageCaptureFromContext(ctx)
	if c == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.err != nil {
		return
	}
	decision := c.pricing[selected]
	if selected == nil || search == nil || decision == nil {
		c.err = fmt.Errorf("%w: search surcharge has no observed token price", ErrMigrationUsageCaptureIncomplete)
		return
	}
	expected := decision.Cost
	expected.TotalCost += search.TotalCost
	expected.ActualCost += search.ActualCost
	if migrationCanonicalTokenCost(*selected) != expected {
		c.err = fmt.Errorf("%w: selected price changed outside search surcharge calculation", ErrMigrationUsageCaptureIncomplete)
		return
	}
	price := defaultSearchPricePer1k
	if pricePer1K != nil {
		price = *pricePer1K
	}
	decision.SearchSurcharge = &migrationSearchPricing{Calls: calls, PricePer1K: price, RateMultiplier: rateMultiplier, Cost: *search}
	decision.Cost = expected
}

// calculatedPricingLocked is called only while capture.mu is held. The pointer
// identity and result equality bind pricing to the final selected calculation.
func (c *MigrationUsageCapture) calculatedPricingLocked(cost *CostBreakdown) (*migrationTokenPricingDecision, error) {
	decision := c.pricing[cost]
	if cost == nil || decision == nil || decision.Model == "" || decision.PricingAt.IsZero() {
		return nil, fmt.Errorf("%w: final settlement has no observed actual token pricing", ErrMigrationUsageCaptureIncomplete)
	}
	if migrationCanonicalTokenCost(*cost) != decision.Cost {
		return nil, fmt.Errorf("%w: final cost differs from observed pricing calculation", ErrMigrationUsageCaptureIncomplete)
	}
	return decision, nil
}
