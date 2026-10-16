package service

import (
	"context"
	"errors"
)

// BeginMigrationQuiet preserves plugin functionality on resume. Active plugin
// runtimes may issue host RPCs without an HTTP handler, so they are explicitly
// unsupported by this fence until their separate RPC drain is implemented.
func (m *PluginManager) BeginMigrationQuiet(ctx context.Context) (func(), error) {
	if m == nil {
		return nil, errors.New("migration plugin manager is unavailable")
	}
	resume, err := m.migrationWorkGate.Pause(ctx)
	if err != nil {
		return resume, err
	}
	m.mu.Lock()
	active := len(m.runtimes)
	m.mu.Unlock()
	if active != 0 {
		return resume, errors.New("migration plugin host RPC fence is not implemented for active plugin runtimes")
	}
	return resume, nil
}

func (m *PluginManager) MigrationTaskStats() MigrationTaskStats {
	if m == nil {
		return MigrationTaskStats{}
	}
	stats := m.migrationWorkGate.Stats()
	m.mu.Lock()
	stats.Pending = uint64(len(m.runtimes))
	m.mu.Unlock()
	return stats
}
