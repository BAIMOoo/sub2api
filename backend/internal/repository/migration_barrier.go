package repository

import (
	"context"
	"errors"
	"sync"
	"time"
)

// migrationBarrier closes admission before waiting for existing operations.
// An unsuccessful pause deliberately remains closed: only an explicit resume
// may reopen admission after the operator has established the authoritative end.
type migrationBarrier struct {
	mu       sync.Mutex
	paused   bool
	active   int
	pausedAt time.Time
	resumed  chan struct{}
	drained  chan struct{}
}

type migrationBarrierStatus struct {
	Paused   bool      `json:"paused"`
	Active   int       `json:"active_operations"`
	PausedAt time.Time `json:"paused_at,omitempty"`
}

func (b *migrationBarrier) acquire(ctx context.Context) (func(), error) {
	lease, err := b.acquireLease(ctx)
	if err != nil {
		return nil, err
	}
	return lease.release, nil
}

type migrationBarrierLease struct {
	barrier  *migrationBarrier
	released bool // protected by barrier.mu
}

func (l *migrationBarrierLease) releaseLocked() {
	if l.released {
		return
	}
	l.released = true
	l.barrier.active--
	if l.barrier.paused && l.barrier.active == 0 {
		close(l.barrier.drained)
	}
}

func (l *migrationBarrierLease) release() {
	l.barrier.mu.Lock()
	defer l.barrier.mu.Unlock()
	l.releaseLocked()
}

// releaseIfOpen decides whether a session may return to the pool atomically
// with pause. A session rejected here keeps its lease through physical Close.
func (l *migrationBarrierLease) releaseIfOpen() bool {
	l.barrier.mu.Lock()
	defer l.barrier.mu.Unlock()
	if l.barrier.paused {
		return false
	}
	l.releaseLocked()
	return true
}

func (b *migrationBarrier) acquireLease(ctx context.Context) (*migrationBarrierLease, error) {
	for {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		b.mu.Lock()
		if !b.paused {
			b.active++
			b.mu.Unlock()
			return &migrationBarrierLease{barrier: b}, nil
		}
		resumed := b.resumed
		b.mu.Unlock()
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-resumed:
		}
	}
}

func (b *migrationBarrier) pause(ctx context.Context) error {
	b.mu.Lock()
	if !b.paused {
		b.paused = true
		b.pausedAt = time.Now().UTC()
		b.resumed = make(chan struct{})
		b.drained = make(chan struct{})
		if b.active == 0 {
			close(b.drained)
		}
	}
	drained := b.drained
	b.mu.Unlock()
	select {
	case <-drained:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (b *migrationBarrier) resume() {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.paused {
		b.paused = false
		b.pausedAt = time.Time{}
		close(b.resumed)
	}
}

func (b *migrationBarrier) status() migrationBarrierStatus {
	b.mu.Lock()
	defer b.mu.Unlock()
	return migrationBarrierStatus{Paused: b.paused, Active: b.active, PausedAt: b.pausedAt}
}

func (b *migrationBarrier) requireDrained() error {
	return b.whileDrained(func() error { return nil })
}

// whileDrained serializes the handover decision with reopening admission.
// The callback must not acquire this barrier or run network I/O.
func (b *migrationBarrier) whileDrained(change func() error) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if !b.paused || b.active != 0 {
		return errors.New("data switch requires paused admission and zero active operations")
	}
	return change()
}
