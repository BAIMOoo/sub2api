package service

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// MigrationUsageRuntime owns recovery and replay separately from HTTP capture.
// Capture can only be enabled by a verified legacy-writer handover callback.
// Starting this worker alone does not make any transport or data cutover ready.
type MigrationUsageRuntime struct {
	mu            sync.Mutex
	journal       *MigrationUsageJournal
	settlement    MigrationUsageSettlement
	reconcile     MigrationUsageReconciler
	legacyDrained func(context.Context) error
	capturing     bool
	closed        bool
	lastErr       error
	workerCancel  context.CancelFunc
	workerDone    chan struct{}
}

var migrationUsageHTTPState struct {
	sync.RWMutex
	runtime *MigrationUsageRuntime
}

func MigrationUsageConfigured() (bool, error) {
	raw := strings.TrimSpace(os.Getenv("MIGRATION_USAGE_ENABLED"))
	if raw == "" {
		return false, nil
	}
	enabled, err := strconv.ParseBool(raw)
	if err != nil {
		return false, errors.New("MIGRATION_USAGE_ENABLED must be a boolean")
	}
	return enabled, nil
}

// MigrationUsageHTTPConfig leaves the existing HTTP path untouched until the
// explicitly configured runtime has passed its legacy-writer handover. Missing
// or malformed configured dependencies fail closed at the handler, not by
// silently returning to asynchronous billing.
func MigrationUsageHTTPConfig() (*MigrationUsageJournal, MigrationUsageCaptureOptions, bool) {
	enabled, err := MigrationUsageConfigured()
	if err != nil {
		return nil, MigrationUsageCaptureOptions{}, true
	}
	if !enabled {
		return nil, MigrationUsageCaptureOptions{}, false
	}
	migrationUsageHTTPState.RLock()
	runtime := migrationUsageHTTPState.runtime
	migrationUsageHTTPState.RUnlock()
	if runtime == nil {
		return nil, MigrationUsageCaptureOptions{}, true
	}
	runtime.mu.Lock()
	defer runtime.mu.Unlock()
	if runtime.closed {
		return nil, MigrationUsageCaptureOptions{}, true
	}
	if !runtime.capturing {
		return nil, MigrationUsageCaptureOptions{}, false
	}
	return runtime.journal, MigrationUsageCaptureOptions{PlatformQuotaDBAuthoritative: true}, true
}

func NewMigrationUsageRuntime(journal *MigrationUsageJournal, settlement MigrationUsageSettlement,
	reconcile MigrationUsageReconciler, legacyDrained func(context.Context) error) (*MigrationUsageRuntime, error) {
	if journal == nil || settlement == nil || reconcile == nil {
		return nil, errors.New("migration usage runtime requires journal, settlement and cache reconciliation")
	}
	return &MigrationUsageRuntime{journal: journal, settlement: settlement,
		reconcile: reconcile, legacyDrained: legacyDrained}, nil
}

// RecoverAndStart must finish before the business listener accepts traffic.
// Existing intents are settled and reconciled first, even in the initial
// capture-disabled coexistence phase. Close waits for this owned replay worker.
func (m *MigrationUsageRuntime) RecoverAndStart(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.closed || m.workerDone != nil {
		return errors.New("migration usage runtime cannot start twice")
	}
	if _, err := m.journal.Replay(ctx, m.settlement, m.reconcile); err != nil {
		m.lastErr = err
		return err
	}
	workerCtx, cancel := context.WithCancel(context.Background())
	m.workerCancel, m.workerDone = cancel, make(chan struct{})
	go m.replayLoop(workerCtx)
	return nil
}

func (m *MigrationUsageRuntime) replayLoop(ctx context.Context) {
	defer close(m.workerDone)
	timer := time.NewTicker(100 * time.Millisecond)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
			replayCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
			_, err := m.journal.Replay(replayCtx, m.settlement, m.reconcile)
			cancel()
			m.mu.Lock()
			m.lastErr = err
			m.mu.Unlock()
		}
	}
}

// EnableCapture has no environment-flag or unverified-boolean bypass. The
// callback must prove all old Redis->DB quota flushers and legacy billing
// writers are drained/fenced. A missing integration keeps capture disabled.
func (m *MigrationUsageRuntime) EnableCapture(ctx context.Context) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.closed || m.workerDone == nil {
		return errors.New("migration usage replay is not running")
	}
	if m.capturing {
		return nil
	}
	if m.legacyDrained == nil {
		return errors.New("migration legacy writer proof is not connected")
	}
	if err := m.legacyDrained(ctx); err != nil {
		return err
	}
	if _, err := m.journal.Replay(ctx, m.settlement, m.reconcile); err != nil {
		m.lastErr = err
		return err
	}
	m.capturing = true
	return nil
}

// InstallMigrationUsageHTTPRuntime is a startup-only operation. Replacing a
// live journal could orphan accepted events, so a second installation fails.
func InstallMigrationUsageHTTPRuntime(m *MigrationUsageRuntime) error {
	if m == nil {
		return errors.New("migration usage runtime is required")
	}
	migrationUsageHTTPState.Lock()
	defer migrationUsageHTTPState.Unlock()
	if migrationUsageHTTPState.runtime != nil {
		return errors.New("migration usage runtime already installed")
	}
	migrationUsageHTTPState.runtime = m
	return nil
}

func (m *MigrationUsageRuntime) Close() error {
	m.mu.Lock()
	if m.closed {
		m.mu.Unlock()
		return nil
	}
	m.closed = true
	cancel, done := m.workerCancel, m.workerDone
	m.mu.Unlock()
	if cancel != nil {
		cancel()
		<-done
	}
	return m.journal.Close()
}

func MigrationUsageJournalPathFromEnv() (string, error) {
	path := strings.TrimSpace(os.Getenv("MIGRATION_USAGE_JOURNAL_DIR"))
	if !filepath.IsAbs(path) || filepath.Clean(path) == "/" {
		return "", errors.New("MIGRATION_USAGE_JOURNAL_DIR requires an absolute persistent directory")
	}
	return path, nil
}
