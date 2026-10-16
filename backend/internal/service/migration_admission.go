package service

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"sync"
	"time"
)

type migrationIngressState struct {
	paused   bool
	epoch    uint64
	queued   int64
	pausedAt time.Time
}

type MigrationIngressSnapshot struct {
	Paused         bool      `json:"paused"`
	Epoch          uint64    `json:"epoch"`
	QueuedRequests int64     `json:"queued_requests"`
	PausedAt       time.Time `json:"paused_at,omitempty"`
	Quiet          bool      `json:"quiet"`
}

func (m *MigrationRuntime) ingressSnapshotLocked() MigrationIngressSnapshot {
	return MigrationIngressSnapshot{Paused: m.ingress.paused, Epoch: m.ingress.epoch,
		QueuedRequests: m.ingress.queued, PausedAt: m.ingress.pausedAt,
		Quiet: m.ingress.paused && m.requests == 0 && m.hijacked == 0}
}

// PauseIngress stops new business-handler admission, including requests using
// an already-established HTTP keepalive connection. Existing HTTP/SSE/WS work
// proceeds unchanged. Control requests use their separate listener.
// No timeout automatically reopens admission against an uncertain data primary.
func (m *MigrationRuntime) PauseIngress() MigrationIngressSnapshot {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.ingress.paused {
		m.ingress.paused = true
		m.ingress.epoch++
		m.ingress.pausedAt = time.Now()
		m.notifyLocked()
	}
	return m.ingressSnapshotLocked()
}

// SetIngressResumeGuard is startup-only and runs before admission reopens.
func (m *MigrationRuntime) SetIngressResumeGuard(guard func() error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if guard == nil || m.ingressResumeGuard != nil {
		panic("migration ingress resume guard duplicated or absent")
	}
	m.ingressResumeGuard = guard
}

func (m *MigrationRuntime) ResumeIngress(expectedEpoch uint64) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.ingress.epoch != expectedEpoch {
		return errors.New("migration ingress epoch mismatch")
	}
	if m.ingress.paused {
		if m.ingressResumeGuard != nil {
			if err := m.ingressResumeGuard(); err != nil {
				return err
			}
		}
		m.ingress.paused = false
		m.ingress.epoch++
		m.ingress.pausedAt = time.Time{}
		m.notifyLocked()
	}
	return nil
}

func (m *MigrationRuntime) admitMigrationRequest(ctx context.Context) bool {
	m.mu.Lock()
	queued := false
	for m.ingress.paused {
		if !queued {
			m.ingress.queued++
			queued = true
			m.notifyLocked()
		}
		changed := m.changed
		m.mu.Unlock()
		select {
		case <-ctx.Done():
			m.mu.Lock()
			m.ingress.queued--
			m.notifyLocked()
			m.mu.Unlock()
			return false
		case <-changed:
		}
		m.mu.Lock()
	}
	if queued {
		m.ingress.queued--
	}
	if ctx.Err() != nil {
		m.notifyLocked()
		m.mu.Unlock()
		return false
	}
	m.requests++
	m.notifyLocked()
	m.mu.Unlock()
	return true
}

func (m *MigrationRuntime) AssertIngressQuiet() error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.ingress.paused || m.requests != 0 || m.hijacked != 0 {
		return errors.New("migration ingress is not paused and fully drained")
	}
	return nil
}

var migrationIngressOwner struct {
	sync.RWMutex
	runtime *MigrationRuntime
}

// AttachMigrationIngressRuntime is called once at application startup. The
// check proves only public-handler quiescence; writer fences remain separate.
func AttachMigrationIngressRuntime(m *MigrationRuntime) error {
	if m == nil {
		return errors.New("migration runtime is required")
	}
	migrationIngressOwner.Lock()
	defer migrationIngressOwner.Unlock()
	if migrationIngressOwner.runtime != nil {
		return errors.New("migration ingress runtime already attached")
	}
	migrationIngressOwner.runtime = m
	return nil
}

func MigrationIngressQuiet() error {
	migrationIngressOwner.RLock()
	m := migrationIngressOwner.runtime
	migrationIngressOwner.RUnlock()
	if m == nil {
		return errors.New("migration ingress proof is not connected")
	}
	return m.AssertIngressQuiet()
}

func (m *MigrationRuntime) ingressControlHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /data/ingress/status", func(w http.ResponseWriter, r *http.Request) {
		m.mu.Lock()
		snapshot := m.ingressSnapshotLocked()
		m.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(snapshot)
	})
	mux.HandleFunc("POST /data/ingress/pause", func(w http.ResponseWriter, r *http.Request) {
		unlock := LockMigrationTransition()
		defer unlock()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(m.PauseIngress())
	})
	mux.HandleFunc("POST /data/ingress/resume", func(w http.ResponseWriter, r *http.Request) {
		var request struct {
			ExpectedEpoch *uint64 `json:"expected_epoch"`
		}
		dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1024))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&request); err != nil || request.ExpectedEpoch == nil {
			http.Error(w, "invalid migration ingress resume request", http.StatusBadRequest)
			return
		}
		var trailing any
		if err := dec.Decode(&trailing); err != io.EOF {
			http.Error(w, "invalid migration ingress resume request", http.StatusBadRequest)
			return
		}
		unlock := LockMigrationTransition()
		defer unlock()
		if err := m.ResumeIngress(*request.ExpectedEpoch); err != nil {
			http.Error(w, "migration ingress phase mismatch", http.StatusConflict)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
	return mux
}
