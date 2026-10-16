package service

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func migrationUsageTestEvent(t *testing.T, requestID string) *MigrationUsageEvent {
	t.Helper()
	at := time.Date(2026, 10, 16, 1, 2, 3, 0, time.UTC)
	event, err := NewMigrationUsageEvent(UsageBillingCommand{
		RequestID: requestID, APIKeyID: 2, UserID: 1, AccountID: 3,
		Model: "test-model", InputTokens: 10, OutputTokens: 20, BalanceCost: .25,
	}, UsageLog{
		UserID: 1, APIKeyID: 2, AccountID: 3, RequestID: requestID,
		Model: "test-model", InputTokens: 10, OutputTokens: 20,
		TotalCost: .125, ActualCost: .25, RateMultiplier: 2, CreatedAt: at,
	}, MigrationUsagePricingSnapshot{
		PricingAt: at, BillingModel: "test-model", RateMultiplier: 2, AccountRateMultiplier: 1,
		Cost:            CostBreakdown{TotalCost: .125, ActualCost: .25},
		ResolvedPricing: json.RawMessage(`{"input_per_million":1000,"output_per_million":5750}`),
	}, "", 0)
	require.NoError(t, err)
	return event
}

type migrationUsageTestSettlement struct {
	seen         map[string]string
	charged      float64
	logs         int
	unknownOnce  bool
	settlementFn func()
}

func (s *migrationUsageTestSettlement) ApplyMigrationUsage(_ context.Context, event *MigrationUsageEvent) (*UsageBillingApplyResult, error) {
	if s.settlementFn != nil {
		s.settlementFn()
	}
	if s.seen == nil {
		s.seen = make(map[string]string)
	}
	if existing, ok := s.seen[event.ID]; ok {
		if existing != event.Command.RequestFingerprint {
			return nil, ErrUsageBillingRequestConflict
		}
		return &UsageBillingApplyResult{}, nil
	}
	s.seen[event.ID] = event.Command.RequestFingerprint
	s.charged += event.Command.BalanceCost
	s.logs++
	if s.unknownOnce {
		s.unknownOnce = false
		return nil, errors.New("COMMIT completed but response was lost")
	}
	return &UsageBillingApplyResult{Applied: true}, nil
}

func migrationUsageTestReconcile(context.Context, *MigrationUsageEvent) error { return nil }

func TestMigrationUsageJournalCrashRecovery(t *testing.T) {
	const childEnv = "SUB2API_MIGRATION_USAGE_CRASH_TEST"
	if dir := os.Getenv(childEnv); dir != "" {
		journal, err := OpenMigrationUsageJournal(dir)
		require.NoError(t, err)
		require.NoError(t, journal.Append(migrationUsageTestEvent(t, "crashed")))
		// Exit without Close/replay to model a process dying after acknowledged
		// persistence and before the in-memory worker receives the event.
		os.Exit(0)
	}
	dir := filepath.Join(t.TempDir(), "journal")
	cmd := exec.Command(os.Args[0], "-test.run=^TestMigrationUsageJournalCrashRecovery$")
	cmd.Env = append(os.Environ(), childEnv+"="+dir)
	out, err := cmd.CombinedOutput()
	require.NoError(t, err, string(out))
	journal, err := OpenMigrationUsageJournal(dir)
	require.NoError(t, err)
	t.Cleanup(func() { require.NoError(t, journal.Close()) })
	pending, err := journal.Pending()
	require.NoError(t, err)
	require.Len(t, pending, 1)
	require.Equal(t, migrationUsageTestEvent(t, "crashed"), pending[0])
	sink := &migrationUsageTestSettlement{}
	count, err := journal.Replay(context.Background(), sink, migrationUsageTestReconcile)
	require.NoError(t, err)
	require.Equal(t, 1, count)
	require.Equal(t, .25, sink.charged)
	pending, err = journal.Pending()
	require.NoError(t, err)
	require.Empty(t, pending)
}

func TestMigrationUsageJournalUnknownCommitAndDuplicateReplay(t *testing.T) {
	journal, err := OpenMigrationUsageJournal(t.TempDir())
	require.NoError(t, err)
	defer journal.Close()
	event := migrationUsageTestEvent(t, "unknown-commit")
	require.NoError(t, journal.Append(event))
	sink := &migrationUsageTestSettlement{unknownOnce: true}
	count, err := journal.Replay(context.Background(), sink, migrationUsageTestReconcile)
	require.ErrorContains(t, err, "response was lost")
	require.Zero(t, count)
	pending, err := journal.Pending()
	require.NoError(t, err)
	require.Len(t, pending, 1)
	count, err = journal.Replay(context.Background(), sink, migrationUsageTestReconcile)
	require.NoError(t, err)
	require.Equal(t, 1, count)
	// Even if a removed event reappears after recovery/copying from the old
	// server, the stable dedup key prevents another charge and usage log.
	require.NoError(t, journal.Append(event))
	_, err = journal.Replay(context.Background(), sink, migrationUsageTestReconcile)
	require.NoError(t, err)
	require.Equal(t, .25, sink.charged)
	require.Equal(t, 1, sink.logs)
}

func TestMigrationUsageJournalRetainsEventUntilCachesReconciled(t *testing.T) {
	journal, err := OpenMigrationUsageJournal(t.TempDir())
	require.NoError(t, err)
	defer journal.Close()
	require.NoError(t, journal.Append(migrationUsageTestEvent(t, "cache-failure")))
	sink := &migrationUsageTestSettlement{}
	_, err = journal.Replay(context.Background(), sink, func(context.Context, *MigrationUsageEvent) error { return errors.New("Redis unavailable") })
	require.ErrorContains(t, err, "Redis unavailable")
	pending, err := journal.Pending()
	require.NoError(t, err)
	require.Len(t, pending, 1)
	_, err = journal.Replay(context.Background(), sink, migrationUsageTestReconcile)
	require.NoError(t, err)
	require.Equal(t, .25, sink.charged)
}

func TestMigrationUsageJournalDiskFailureNeverAcknowledgesAppend(t *testing.T) {
	for _, failAt := range []string{"file-sync", "directory-sync"} {
		t.Run(failAt, func(t *testing.T) {
			journal, err := OpenMigrationUsageJournal(t.TempDir())
			require.NoError(t, err)
			defer journal.Close()
			diskErr := errors.New("simulated EIO/ENOSPC")
			if failAt == "file-sync" {
				journal.syncFile = func(*os.File) error { return diskErr }
			} else {
				journal.syncDir = func(string) error { return diskErr }
			}
			require.ErrorIs(t, journal.Append(migrationUsageTestEvent(t, failAt)), diskErr)
			require.ErrorContains(t, journal.Append(migrationUsageTestEvent(t, "later")), "requires recovery")
		})
	}
}

func TestMigrationUsageJournalRejectsConflictsCorruptionAndDoubleOpen(t *testing.T) {
	dir := t.TempDir()
	journal, err := OpenMigrationUsageJournal(dir)
	require.NoError(t, err)
	defer journal.Close()
	_, err = OpenMigrationUsageJournal(dir)
	require.Error(t, err)
	event := migrationUsageTestEvent(t, "collision")
	require.NoError(t, journal.Append(event))
	require.NoError(t, journal.Append(event))
	conflict := *event
	conflict.Platform = "different"
	conflict.Command.RequestFingerprint, err = conflict.fingerprint()
	require.NoError(t, err)
	require.ErrorIs(t, journal.Append(&conflict), ErrUsageBillingRequestConflict)
	path := filepath.Join(dir, event.ID+".json")
	require.NoError(t, os.WriteFile(path, []byte(`{"Checksum":"invalid","Payload":"e30="}`), 0600))
	_, err = journal.Pending()
	require.ErrorContains(t, err, "checksum mismatch")
}

func TestMigrationUsageJournalReplayDoesNotBlockNewPersistence(t *testing.T) {
	journal, err := OpenMigrationUsageJournal(t.TempDir())
	require.NoError(t, err)
	defer journal.Close()
	require.NoError(t, journal.Append(migrationUsageTestEvent(t, "first")))
	entered, release := make(chan struct{}), make(chan struct{})
	sink := &migrationUsageTestSettlement{settlementFn: func() { close(entered); <-release }}
	finished := make(chan error, 1)
	go func() {
		_, err := journal.Replay(context.Background(), sink, migrationUsageTestReconcile)
		finished <- err
	}()
	<-entered
	require.NoError(t, journal.Append(migrationUsageTestEvent(t, "second")))
	close(release)
	require.NoError(t, <-finished)
	pending, err := journal.Pending()
	require.NoError(t, err)
	require.Len(t, pending, 1)
	require.Equal(t, "second", pending[0].Command.RequestID)
}

func TestMigrationUsageEventFreezesSnapshotsAndRejectsMutation(t *testing.T) {
	event := migrationUsageTestEvent(t, "immutable")
	event.Usage.ActualCost = 99
	require.ErrorIs(t, event.Validate(), ErrUsageBillingRequestConflict)
	event = migrationUsageTestEvent(t, "association")
	event.Usage.APIKey = &APIKey{Key: "must-not-be-persisted"}
	require.ErrorContains(t, event.Validate(), "credential associations")
}
