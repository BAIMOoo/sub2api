package service

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func TestMigrationUsageRuntimeRecoveryBeforeCapture(t *testing.T) {
	journal, err := OpenMigrationUsageJournal(t.TempDir())
	require.NoError(t, err)
	require.NoError(t, journal.Append(migrationUsageTestEvent(t, "runtime-recovery")))
	sink := &migrationUsageTestSettlement{unknownOnce: true}
	var drained atomic.Bool
	runtime, err := NewMigrationUsageRuntime(journal, sink, migrationUsageTestReconcile, func(context.Context) error {
		if !drained.Load() {
			return errors.New("legacy writer is still active")
		}
		return nil
	})
	require.NoError(t, err)
	t.Cleanup(func() { require.NoError(t, runtime.Close()) })
	require.ErrorContains(t, runtime.RecoverAndStart(context.Background()), "response was lost")
	require.ErrorContains(t, runtime.EnableCapture(context.Background()), "not running")
	pending, err := journal.Pending()
	require.NoError(t, err)
	require.Len(t, pending, 1)
	require.NoError(t, runtime.RecoverAndStart(context.Background()))
	require.Equal(t, .25, sink.charged)
	require.ErrorContains(t, runtime.EnableCapture(context.Background()), "legacy writer")
	drained.Store(true)
	require.NoError(t, runtime.EnableCapture(context.Background()))
	require.ErrorContains(t, runtime.RecoverAndStart(context.Background()), "twice")
	require.NoError(t, runtime.Close())
	require.ErrorContains(t, runtime.EnableCapture(context.Background()), "not running")
}

func TestMigrationUsageRuntimeRefusesUnwiredLegacyProof(t *testing.T) {
	journal, err := OpenMigrationUsageJournal(t.TempDir())
	require.NoError(t, err)
	runtime, err := NewMigrationUsageRuntime(journal, &migrationUsageTestSettlement{}, migrationUsageTestReconcile, nil)
	require.NoError(t, err)
	defer runtime.Close()
	require.NoError(t, runtime.RecoverAndStart(context.Background()))
	require.ErrorContains(t, runtime.EnableCapture(context.Background()), "not connected")
}

func TestMigrationUsageRuntimeCloseCancelsReplayAndKeepsIntent(t *testing.T) {
	journal, err := OpenMigrationUsageJournal(t.TempDir())
	require.NoError(t, err)
	entered := make(chan struct{})
	runtime, err := NewMigrationUsageRuntime(journal, &migrationUsageTestSettlement{},
		func(ctx context.Context, event *MigrationUsageEvent) error {
			close(entered)
			<-ctx.Done()
			return ctx.Err()
		}, nil)
	require.NoError(t, err)
	require.NoError(t, runtime.RecoverAndStart(context.Background()))
	require.NoError(t, journal.Append(migrationUsageTestEvent(t, "runtime-cancel")))
	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("replay worker did not begin")
	}
	require.NoError(t, runtime.Close())
	reopened, err := OpenMigrationUsageJournal(journal.dir)
	require.NoError(t, err)
	defer reopened.Close()
	pending, err := reopened.Pending()
	require.NoError(t, err)
	require.Len(t, pending, 1)
}

func TestMigrationUsageHTTPConfigFailsClosedWhenNotInstalled(t *testing.T) {
	t.Setenv("MIGRATION_USAGE_ENABLED", "")
	_, _, enabled := MigrationUsageHTTPConfig()
	require.False(t, enabled)
	t.Setenv("MIGRATION_USAGE_ENABLED", "invalid")
	journal, _, enabled := MigrationUsageHTTPConfig()
	require.True(t, enabled)
	require.Nil(t, journal)
	t.Setenv("MIGRATION_USAGE_ENABLED", "true")
	journal, _, enabled = MigrationUsageHTTPConfig()
	require.True(t, enabled)
	require.Nil(t, journal)
}
