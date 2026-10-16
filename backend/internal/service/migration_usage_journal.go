package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
)

const migrationUsageMaxEventBytes = 2 << 20

type migrationUsageEnvelope struct {
	Checksum string
	Payload  []byte
}

// MigrationUsageJournal persists immutable intent before client completion.
// The directory must be on a durable, fsync-capable local persistent volume.
// It must be reopened and replayed before accepting traffic after a crash.
// There is intentionally no environment switch or background retry goroutine:
// wiring, request gating and lifecycle ownership belong to the caller.
type MigrationUsageJournal struct {
	mu       sync.Mutex
	replayMu sync.Mutex
	dir      string
	lock     *os.File
	poisoned error
	syncFile func(*os.File) error
	syncDir  func(string) error
}

func OpenMigrationUsageJournal(dir string) (*MigrationUsageJournal, error) {
	if !filepath.IsAbs(dir) {
		return nil, errors.New("migration usage journal requires an absolute path")
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, err
	}
	// Persist the directory entry too. Production provisioning should create
	// the mounted journal directory before startup.
	if err := migrationUsageSyncDir(filepath.Dir(dir)); err != nil {
		return nil, err
	}
	lock, err := lockMigrationUsageJournal(filepath.Join(dir, ".lock"))
	if err != nil {
		return nil, err
	}
	j := &MigrationUsageJournal{dir: dir, lock: lock, syncFile: func(f *os.File) error { return f.Sync() }, syncDir: migrationUsageSyncDir}
	if err := j.syncDir(dir); err != nil {
		_ = lock.Close()
		return nil, err
	}
	return j, nil
}

func migrationUsageSyncDir(dir string) error {
	f, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}

func (j *MigrationUsageJournal) Close() error {
	j.mu.Lock()
	defer j.mu.Unlock()
	if j.lock == nil {
		return nil
	}
	err := j.lock.Close()
	j.lock = nil
	return err
}

func (j *MigrationUsageJournal) ready() error {
	if j.lock == nil {
		return errors.New("migration usage journal is closed")
	}
	if j.poisoned != nil {
		return fmt.Errorf("migration usage journal requires recovery: %w", j.poisoned)
	}
	return nil
}

// Append returns success only after the data file and its published directory
// entry are fsynced. Disk errors poison this handle so admission can fail closed.
// A repeated stable ID with different effects is rejected, never overwritten.
func (j *MigrationUsageJournal) Append(event *MigrationUsageEvent) error {
	if err := event.Validate(); err != nil {
		return err
	}
	payload, err := json.Marshal(event)
	if err != nil {
		return err
	}
	if len(payload) > migrationUsageMaxEventBytes {
		return errors.New("migration usage event exceeds size limit")
	}
	sum := sha256.Sum256(payload)
	data, err := json.Marshal(migrationUsageEnvelope{Checksum: hex.EncodeToString(sum[:]), Payload: payload})
	if err != nil {
		return err
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	if err := j.ready(); err != nil {
		return err
	}
	path := filepath.Join(j.dir, event.ID+".json")
	if existing, err := readMigrationUsageEvent(path); err == nil {
		if existing.Command.RequestFingerprint != event.Command.RequestFingerprint {
			return ErrUsageBillingRequestConflict
		}
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		j.poisoned = err
		return err
	}
	f, err := os.CreateTemp(j.dir, ".pending-")
	if err != nil {
		j.poisoned = err
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(data); err == nil {
		err = j.syncFile(f)
	}
	closeErr := f.Close()
	if err == nil {
		err = closeErr
	}
	if err == nil {
		// Link publishes atomically without replacing an existing event. Both
		// files are on the same filesystem; an unfinished temp is never replayed.
		err = os.Link(f.Name(), path)
	}
	if err == nil {
		err = j.syncDir(j.dir)
	}
	if err != nil {
		j.poisoned = err
	}
	return err
}

func readMigrationUsageEvent(path string) (*MigrationUsageEvent, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Size() > migrationUsageMaxEventBytes*2 {
		return nil, errors.New("invalid migration usage event file")
	}
	var envelope migrationUsageEnvelope
	decoder := json.NewDecoder(f)
	if err := decoder.Decode(&envelope); err != nil {
		return nil, err
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return nil, errors.New("migration usage journal contains trailing data")
	}
	sum := sha256.Sum256(envelope.Payload)
	if envelope.Checksum != hex.EncodeToString(sum[:]) {
		return nil, errors.New("migration usage journal checksum mismatch")
	}
	var event MigrationUsageEvent
	if err := json.Unmarshal(envelope.Payload, &event); err != nil {
		return nil, err
	}
	if err := event.Validate(); err != nil {
		return nil, err
	}
	if filepath.Base(path) != event.ID+".json" {
		return nil, errors.New("migration usage journal filename mismatch")
	}
	return &event, nil
}

func (j *MigrationUsageJournal) Pending() ([]*MigrationUsageEvent, error) {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.pendingLocked()
}

func (j *MigrationUsageJournal) pendingLocked() ([]*MigrationUsageEvent, error) {
	if err := j.ready(); err != nil {
		return nil, err
	}
	entries, err := os.ReadDir(j.dir)
	if err != nil {
		return nil, err
	}
	events := make([]*MigrationUsageEvent, 0)
	for _, entry := range entries {
		if !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		event, err := readMigrationUsageEvent(filepath.Join(j.dir, entry.Name()))
		if err != nil {
			return nil, fmt.Errorf("migration usage recovery %s: %w", entry.Name(), err)
		}
		events = append(events, event)
	}
	// Preserve the original settlement order when replaying rolling windows.
	// Traffic admission must remain gated until startup recovery has completed.
	sort.Slice(events, func(i, k int) bool {
		if events[i].Usage.CreatedAt.Equal(events[k].Usage.CreatedAt) {
			return events[i].ID < events[k].ID
		}
		return events[i].Usage.CreatedAt.Before(events[k].Usage.CreatedAt)
	})
	return events, nil
}

// Replay stops on the first uncertain settlement or cache reconciliation. It
// never acknowledges on error. SQL deduplication resolves unknown commit
// outcomes; rerunning an arbitrary monetary UPDATE is not safe recovery.
func (j *MigrationUsageJournal) Replay(ctx context.Context, settlement MigrationUsageSettlement, reconcile MigrationUsageReconciler) (int, error) {
	if settlement == nil || reconcile == nil {
		return 0, errors.New("migration usage replay requires atomic settlement and idempotent reconciliation")
	}
	j.replayMu.Lock()
	defer j.replayMu.Unlock()
	events, err := j.Pending()
	if err != nil {
		return 0, err
	}
	completed := 0
	for _, event := range events {
		if err := ctx.Err(); err != nil {
			return completed, err
		}
		if _, err := settlement.ApplyMigrationUsage(ctx, event); err != nil {
			return completed, err
		}
		if err := reconcile(ctx, event); err != nil {
			return completed, err
		}
		if err := j.acknowledge(event); err != nil {
			return completed, err
		}
		completed++
	}
	return completed, nil
}

func (j *MigrationUsageJournal) acknowledge(event *MigrationUsageEvent) error {
	j.mu.Lock()
	defer j.mu.Unlock()
	if err := j.ready(); err != nil {
		return err
	}
	if err := os.Remove(filepath.Join(j.dir, event.ID+".json")); err != nil {
		j.poisoned = err
		return err
	}
	if err := j.syncDir(j.dir); err != nil {
		j.poisoned = err
		return err
	}
	return nil
}
