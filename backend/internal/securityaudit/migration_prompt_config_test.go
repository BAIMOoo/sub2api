package securityaudit

import (
	"context"
	"encoding/json"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestMigrationConfigQuietReturnsResumeOnTimeoutAndAllowsExplicitRebuild(t *testing.T) {
	m := NewConfigManager(nil, staticSettingRepository{values: map[string]string{}}, nil, prefixEncryptor{}, testTotpKeyConfig())
	release, ok := m.refreshGate.TryEnter()
	require.True(t, ok)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	resume, err := m.BeginMigrationConfigQuiet(ctx)
	require.ErrorIs(t, err, context.DeadlineExceeded)
	require.NotNil(t, resume)
	_, ok = m.refreshGate.TryEnter()
	require.False(t, ok)
	release()
	require.NoError(t, m.RebuildMigrationSnapshot(context.Background()))
	_, ok = m.Active()
	require.True(t, ok)
	resume()
	release, ok = m.refreshGate.TryEnter()
	require.True(t, ok)
	release()
}

type migrationSerializedSettings struct {
	service.SettingRepository
	calls   atomic.Int64
	entered chan struct{}
	release chan struct{}
}

func (r *migrationSerializedSettings) GetMultiple(context.Context, []string) (map[string]string, error) {
	version := r.calls.Add(1)
	if version == 1 {
		close(r.entered)
		<-r.release
	}
	storage := DefaultStorageConfig()
	storage.ConfigVersion = version
	raw, _ := json.Marshal(storage)
	return map[string]string{SettingKeyPromptAuditConfig: string(raw), SettingKeyRiskControl: "false"}, nil
}

func TestMigrationConfigSnapshotCannotBeRepopulatedByOldReload(t *testing.T) {
	repo := &migrationSerializedSettings{entered: make(chan struct{}), release: make(chan struct{})}
	m := NewConfigManager(nil, repo, nil, prefixEncryptor{}, testTotpKeyConfig())
	oldDone := make(chan error, 1)
	go func() { oldDone <- m.Reload(context.Background()) }()
	<-repo.entered
	rebuilt := make(chan error, 1)
	go func() { rebuilt <- m.RebuildMigrationSnapshot(context.Background()) }()
	// The rebuild cannot install a new snapshot before the old read returns;
	// otherwise the late old read could overwrite the new generation.
	select {
	case err := <-rebuilt:
		t.Fatalf("rebuild passed active old reload: %v", err)
	case <-time.After(10 * time.Millisecond):
	}
	close(repo.release)
	require.NoError(t, <-oldDone)
	require.NoError(t, <-rebuilt)
	active, ok := m.Active()
	require.True(t, ok)
	require.EqualValues(t, 2, active.ConfigVersion)
}
