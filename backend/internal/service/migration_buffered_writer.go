package service

import (
	"context"
	"errors"
	"sync"
)

type migrationFlushRequest struct {
	ctx     context.Context
	release <-chan struct{}
	done    chan error
}

// Tracks accepted rows across both the public queue and the writer's local
// batch. A failed/discarded batch is a sticky handover blocker: waiting for an
// empty queue must never erase evidence of an unpersisted accepted write.
type migrationBufferedWriter struct {
	producerMu sync.Mutex
	paused     bool
	held       []func()
	mu         sync.Mutex
	pending    uint64
	active     uint64
	failed     uint64
	lastErr    error
	requests   chan migrationFlushRequest
}

// Post-fence observations are retained separately from writes accepted before
// the cut point. They cannot enter a database consumer until resume, including
// warnings produced by the handover itself. The bounded hold uses the sink's
// existing overload policy, and resume replays through its normal queue.
func (w *migrationBufferedWriter) enqueue(limit int, submit func()) bool {
	w.producerMu.Lock()
	defer w.producerMu.Unlock()
	if w.paused {
		if len(w.held) >= limit {
			return false
		}
		w.held = append(w.held, submit)
		return true
	}
	submit()
	return true
}

func (w *migrationBufferedWriter) heldCount() uint64 {
	w.producerMu.Lock()
	defer w.producerMu.Unlock()
	return uint64(len(w.held))
}

func (w *migrationBufferedWriter) init()   { w.requests = make(chan migrationFlushRequest) }
func (w *migrationBufferedWriter) accept() { w.mu.Lock(); w.pending++; w.mu.Unlock() }
func (w *migrationBufferedWriter) discard(n uint64) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if n > w.pending {
		n = w.pending
	}
	w.pending -= n
}
func (w *migrationBufferedWriter) start() { w.mu.Lock(); w.active++; w.mu.Unlock() }
func (w *migrationBufferedWriter) finish(n uint64, err error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.active > 0 {
		w.active--
	}
	if n > w.pending {
		n = w.pending
	}
	w.pending -= n
	if err != nil {
		w.failed++
		w.lastErr = err
	}
}
func (w *migrationBufferedWriter) snapshot() MigrationTaskStats {
	w.mu.Lock()
	defer w.mu.Unlock()
	return MigrationTaskStats{Active: w.active, Pending: w.pending, Failed: w.failed}
}
func (w *migrationBufferedWriter) failure() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.lastErr
}

func (w *migrationBufferedWriter) pauseAndFlush(ctx context.Context, stopped <-chan struct{}) (func(), error) {
	if w.requests == nil {
		return nil, errors.New("buffered writer has no flush controller")
	}
	w.producerMu.Lock()
	if w.paused {
		w.producerMu.Unlock()
		return nil, errors.New("buffered writer producer is already paused")
	}
	w.paused = true
	w.producerMu.Unlock()
	release := make(chan struct{})
	var once sync.Once
	resume := func() {
		once.Do(func() {
			close(release)
			w.producerMu.Lock()
			defer w.producerMu.Unlock()
			for _, submit := range w.held {
				submit()
			}
			w.held = nil
			w.paused = false
		})
	}
	req := migrationFlushRequest{ctx: ctx, release: release, done: make(chan error, 1)}
	select {
	case <-ctx.Done():
		resume()
		return nil, ctx.Err()
	case <-stopped:
		resume()
		return nil, errors.New("buffered writer is stopped")
	case w.requests <- req:
	}
	select {
	case err := <-req.done:
		if err != nil {
			resume()
			return nil, err
		}
		return resume, nil
	case <-ctx.Done():
		resume()
		return nil, ctx.Err()
	case <-stopped:
		resume()
		return nil, errors.New("buffered writer is stopped")
	}
}
