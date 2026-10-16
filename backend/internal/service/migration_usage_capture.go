package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"time"
)

var ErrMigrationUsageCaptureIncomplete = errors.New("migration usage capture is incomplete")

type migrationUsageCaptureContextKey struct{}

// MigrationUsageCaptureOptions contains deployment phase invariants only.
// Pricing comes exclusively from the calculation observed through the capture
// context; transport handlers cannot supply or re-resolve a replacement price.
type MigrationUsageCaptureOptions struct {
	// Deprecated: these compatibility fields are ignored. The billing service
	// observes the actual selected calculation, including candidate prices.
	PricingAt       time.Time
	BillingModel    string
	ResolvedPricing json.RawMessage
	// This can only be true after every legacy Redis->DB quota flusher has
	// drained/stopped and platform caches are invalidated/reloaded from DB.
	// It is a deployment phase invariant, not a local background-worker flag.
	PlatformQuotaDBAuthoritative bool
}

// MigrationUsageCapture redirects the common billing + usage log path to one
// durable event. It does not intercept transport writes: the caller must hold
// success/terminal output, call RecordUsage synchronously, check its returned
// error AND CheckPersisted(), then release that output. It does not imply WS,
// provider coverage, or migration readiness.
type MigrationUsageCapture struct {
	mu       sync.Mutex
	journal  *MigrationUsageJournal
	options  MigrationUsageCaptureOptions
	event    *MigrationUsageEvent
	observed bool
	err      error
	pricing  map[*CostBreakdown]*migrationTokenPricingDecision
}

func WithMigrationUsageCapture(ctx context.Context, journal *MigrationUsageJournal, options MigrationUsageCaptureOptions) (context.Context, *MigrationUsageCapture) {
	if ctx == nil {
		ctx = context.Background()
	}
	options.PricingAt, options.BillingModel, options.ResolvedPricing = time.Time{}, "", nil
	capture := &MigrationUsageCapture{journal: journal, options: options}
	return context.WithValue(ctx, migrationUsageCaptureContextKey{}, capture), capture
}

func migrationUsageCaptureFromContext(ctx context.Context) *MigrationUsageCapture {
	if ctx == nil {
		return nil
	}
	capture, _ := ctx.Value(migrationUsageCaptureContextKey{}).(*MigrationUsageCapture)
	return capture
}

func (c *MigrationUsageCapture) captureBilling(cmd *UsageBillingCommand, usage *UsageLog, params *postUsageBillingParams, deps *billingDeps) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	fail := func(err error) error { c.err = err; return err }
	if c.err != nil {
		return c.err
	}
	if c.journal == nil || cmd == nil || usage == nil || params == nil || params.Cost == nil {
		return fail(fmt.Errorf("%w: missing journal or settlement snapshot", ErrMigrationUsageCaptureIncomplete))
	}
	if c.event != nil {
		return fail(fmt.Errorf("%w: more than one settlement captured for a completion", ErrMigrationUsageCaptureIncomplete))
	}
	platform, platformCost := "", 0.0
	if !params.SimpleModeKeyRateLimitOnly && !params.IsSubscriptionBill && params.Platform != "" && params.Cost.ActualCost > 0 && deps != nil && deps.userPlatformQuotaRepo != nil {
		if !c.options.PlatformQuotaDBAuthoritative {
			return fail(fmt.Errorf("%w: platform quota still requires legacy flusher handoff", ErrMigrationUsageCaptureIncomplete))
		}
		platform, platformCost = params.Platform, params.Cost.ActualCost
	}
	pricing, err := c.calculatedPricingLocked(params.Cost)
	if err != nil {
		return fail(err)
	}
	resolvedPricing, err := json.Marshal(pricing)
	if err != nil {
		return fail(fmt.Errorf("%w: encode observed pricing: %v", ErrMigrationUsageCaptureIncomplete, err))
	}
	event, err := NewMigrationUsageEvent(*cmd, *usage, MigrationUsagePricingSnapshot{
		PricingAt: pricing.PricingAt, BillingModel: pricing.Model,
		ResolvedPricing: resolvedPricing,
		RateMultiplier:  usage.RateMultiplier, AccountRateMultiplier: params.AccountRateMultiplier, Cost: *params.Cost,
	}, platform, platformCost)
	if err != nil {
		return fail(err)
	}
	if err := c.journal.Append(event); err != nil {
		return fail(err)
	}
	c.event = event
	return nil
}

func (c *MigrationUsageCapture) observeUsageLog(usage *UsageLog) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.err != nil {
		return
	}
	if c.event == nil || usage == nil || usage.RequestID != c.event.Command.RequestID || usage.APIKeyID != c.event.Command.APIKeyID {
		c.err = fmt.Errorf("%w: usage-only/simple-mode path is not connected to durable settlement", ErrMigrationUsageCaptureIncomplete)
		return
	}
	c.observed = true
}

func (c *MigrationUsageCapture) CheckPersisted() error {
	if c == nil {
		return ErrMigrationUsageCaptureIncomplete
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.err != nil {
		return c.err
	}
	if c.event == nil || !c.observed {
		return ErrMigrationUsageCaptureIncomplete
	}
	return nil
}

func (c *MigrationUsageCapture) EventID() string {
	if c == nil {
		return ""
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.event == nil {
		return ""
	}
	return c.event.ID
}
