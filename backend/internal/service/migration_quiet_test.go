package service

import (
	"errors"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestMigrationQuietModeRejectsUncontrolledConfiguration(t *testing.T) {
	t.Setenv("MIGRATION_HANDOVER_MODE", "invalid")
	_, err := MigrationRuntimeConfigFromEnv()
	require.ErrorContains(t, err, "HANDOVER_MODE")
	t.Setenv("MIGRATION_HANDOVER_MODE", "quiet")
	t.Setenv("MIGRATION_RUNTIME_ADDR", "")
	_, err = MigrationRuntimeConfigFromEnv()
	require.ErrorContains(t, err, "private")
	t.Setenv("MIGRATION_RUNTIME_ADDR", "127.0.0.1:19000")
	t.Setenv("MIGRATION_RUNTIME_TOKEN", strings.Repeat("x", 32))
	_, err = MigrationRuntimeConfigFromEnv()
	require.NoError(t, err)
}

func TestMigrationQuietReadinessRequiresWritersWithoutPretendingJournalReady(t *testing.T) {
	t.Setenv("MIGRATION_HANDOVER_MODE", "quiet")
	migrationControlRegistry.Lock()
	previous := migrationControlRegistry.readiness
	migrationControlRegistry.readiness = map[string]func() error{"postgres": func() error { return nil }, "redis": func() error { return nil }}
	migrationControlRegistry.Unlock()
	defer func() {
		migrationControlRegistry.Lock()
		migrationControlRegistry.readiness = previous
		migrationControlRegistry.Unlock()
	}()
	state := migrationReadiness()
	_, hasJournal := state["usage_journal"]
	require.False(t, hasJournal)
	require.Contains(t, state, "writer_coverage")
	require.False(t, state["writer_coverage"])
	t.Setenv("MIGRATION_HANDOVER_MODE", "journal")
	state = migrationReadiness()
	require.False(t, state["usage_journal"])
}

func TestMigrationEndpointReadinessDoesNotAcceptMissingOrFailedDataProbe(t *testing.T) {
	migrationControlRegistry.Lock()
	previous := migrationControlRegistry.readiness
	migrationControlRegistry.readiness = map[string]func() error{}
	migrationControlRegistry.Unlock()
	defer func() {
		migrationControlRegistry.Lock()
		migrationControlRegistry.readiness = previous
		migrationControlRegistry.Unlock()
	}()
	require.Error(t, MigrationDataEndpointsReady())
	migrationControlRegistry.Lock()
	migrationControlRegistry.readiness["postgres"] = func() error { return errors.New("paused") }
	migrationControlRegistry.readiness["redis"] = func() error { return nil }
	migrationControlRegistry.Unlock()
	require.Error(t, MigrationDataEndpointsReady())
	migrationControlRegistry.Lock()
	migrationControlRegistry.readiness["postgres"] = func() error { return nil }
	migrationControlRegistry.Unlock()
	require.NoError(t, MigrationDataEndpointsReady())
}
