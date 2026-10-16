package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strings"
	"time"
)

// MigrationUsagePricingSnapshot contains the monetary result calculated using
// request-time pricing. Replay consumes these amounts and never calls pricing
// or a model provider again. ResolvedPricing must contain the resolved unit
// prices/rules captured by the caller, without credentials or request bodies.
type MigrationUsagePricingSnapshot struct {
	PricingAt             time.Time
	BillingModel          string
	RateMultiplier        float64
	AccountRateMultiplier float64
	Cost                  CostBreakdown
	ResolvedPricing       json.RawMessage
}

// MigrationUsageEvent is a frozen settlement intent. It deliberately excludes
// User/APIKey/Account associations: those contain credentials, not settlement
// data. This API is opt-in; constructing a journal does not change live billing.
type MigrationUsageEvent struct {
	Version      int
	ID           string
	Command      UsageBillingCommand
	Usage        UsageLog
	Pricing      MigrationUsagePricingSnapshot
	Platform     string
	PlatformCost float64
}

// MigrationUsageSettlement applies every authoritative DB effect, including the
// usage log and platform quota, in the same deduplicated transaction. A returned
// error, including a lost COMMIT response, must leave the journal event pending.
type MigrationUsageSettlement interface {
	ApplyMigrationUsage(context.Context, *MigrationUsageEvent) (*UsageBillingApplyResult, error)
}

// MigrationUsageReconciler must be safe to repeat after an unknown DB outcome.
// Invalidate/reload derived caches; do not repeat Redis increments or money
// writes here. An event is retained until reconciliation succeeds as well.
type MigrationUsageReconciler func(context.Context, *MigrationUsageEvent) error

func NewMigrationUsageEvent(cmd UsageBillingCommand, usage UsageLog, pricing MigrationUsagePricingSnapshot, platform string, platformCost float64) (*MigrationUsageEvent, error) {
	cmd.Normalize()
	usage.ID = 0
	usage.User, usage.APIKey, usage.Account, usage.Group, usage.Subscription = nil, nil, nil, nil, nil
	usage.RequestID = strings.TrimSpace(usage.RequestID)
	usage.SyncRequestTypeAndLegacyFields()
	event := &MigrationUsageEvent{
		Version: 1, ID: migrationUsageEventID(cmd.RequestID, cmd.APIKeyID),
		Command: cmd, Usage: usage, Pricing: pricing,
		Platform: strings.TrimSpace(platform), PlatformCost: QuantizeUsageBillingAmount(platformCost),
	}
	// A JSON round trip freezes caller-owned pointers and maps. It also rejects
	// NaN/Inf and invalid raw pricing JSON before any disk or DB operation.
	data, err := json.Marshal(event)
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(data, event); err != nil {
		return nil, err
	}
	fingerprint, err := event.fingerprint()
	if err != nil {
		return nil, err
	}
	event.Command.RequestFingerprint = fingerprint
	return event, event.Validate()
}

func migrationUsageEventID(requestID string, apiKeyID int64) string {
	sum := sha256.Sum256([]byte(fmt.Sprintf("%d\x00%s", apiKeyID, requestID)))
	return hex.EncodeToString(sum[:])
}

func (e *MigrationUsageEvent) fingerprint() (string, error) {
	copy := *e
	copy.Command.RequestFingerprint = ""
	data, err := json.Marshal(copy)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(data)
	// usage_billing_dedup.request_fingerprint is VARCHAR(64). Version is
	// already part of the hashed event; adding a textual prefix breaks schema.
	return hex.EncodeToString(sum[:]), nil
}

func (e *MigrationUsageEvent) Validate() error {
	if e == nil || e.Version != 1 || e.Command.RequestID == "" || e.Command.APIKeyID <= 0 || e.Command.UserID <= 0 || e.Command.AccountID <= 0 {
		return errors.New("invalid migration usage identity/version")
	}
	if e.Command.RequestID != strings.TrimSpace(e.Command.RequestID) || e.ID != migrationUsageEventID(e.Command.RequestID, e.Command.APIKeyID) {
		return errors.New("invalid migration usage stable id")
	}
	u := &e.Usage
	if u.User != nil || u.APIKey != nil || u.Account != nil || u.Group != nil || u.Subscription != nil {
		return errors.New("migration usage must not contain credential associations")
	}
	if u.RequestID != e.Command.RequestID || u.APIKeyID != e.Command.APIKeyID || u.UserID != e.Command.UserID || u.AccountID != e.Command.AccountID || u.CreatedAt.IsZero() {
		return errors.New("migration usage log identity/time mismatch")
	}
	if e.Pricing.PricingAt.IsZero() || e.Pricing.BillingModel == "" || len(e.Pricing.ResolvedPricing) == 0 || !json.Valid(e.Pricing.ResolvedPricing) || string(e.Pricing.ResolvedPricing) == "null" {
		return errors.New("migration usage requires request-time resolved pricing snapshot")
	}
	for _, amount := range []float64{e.Command.BalanceCost, e.Command.SubscriptionCost, e.Command.APIKeyQuotaCost, e.Command.APIKeyRateLimitCost, e.Command.AccountQuotaCost, e.PlatformCost} {
		if math.IsNaN(amount) || math.IsInf(amount, 0) || amount < 0 || QuantizeUsageBillingAmount(amount) != amount {
			return errors.New("migration usage contains invalid monetary effect")
		}
	}
	if e.PlatformCost > 0 && (e.Platform == "" || e.Command.SubscriptionCost > 0 || e.Command.BalanceCost <= 0) {
		return errors.New("invalid migration platform quota effect")
	}
	fingerprint, err := e.fingerprint()
	if err != nil {
		return err
	}
	if e.Command.RequestFingerprint != fingerprint {
		return ErrUsageBillingRequestConflict
	}
	return nil
}
