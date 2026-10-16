package service

import (
	"context"
	"errors"
	"sync"
)

// MigrationWorkGate fences a periodic producer and counts work before it is
// accepted/launched. Ticks rejected while paused must remain retryable through
// their existing durable queue or later tick; this is not a drop queue.
// The zero value is ready to use. Pause returns its resume even on timeout.
type MigrationWorkGate struct {
	mu      sync.Mutex
	paused  bool
	active  uint64
	changed chan struct{}
}

func (g *MigrationWorkGate) notifyLocked() {
	if g.changed != nil {
		close(g.changed)
	}
	g.changed = make(chan struct{})
}

func (g *MigrationWorkGate) TryEnter() (func(), bool) {
	g.mu.Lock()
	if g.paused {
		g.mu.Unlock()
		return nil, false
	}
	g.active++
	g.notifyLocked()
	g.mu.Unlock()
	var once sync.Once
	return func() { once.Do(func() { g.mu.Lock(); g.active--; g.notifyLocked(); g.mu.Unlock() }) }, true
}

func (g *MigrationWorkGate) Pause(ctx context.Context) (func(), error) {
	g.mu.Lock()
	if g.paused {
		g.mu.Unlock()
		return nil, errors.New("migration work gate is already paused")
	}
	g.paused = true
	g.notifyLocked()
	g.mu.Unlock()
	var once sync.Once
	resume := func() { once.Do(func() { g.mu.Lock(); g.paused = false; g.notifyLocked(); g.mu.Unlock() }) }
	for {
		g.mu.Lock()
		active, changed := g.active, g.changed
		g.mu.Unlock()
		if active == 0 {
			return resume, nil
		}
		select {
		case <-ctx.Done():
			return resume, ctx.Err()
		case <-changed:
		}
	}
}

func (g *MigrationWorkGate) Stats() MigrationTaskStats {
	g.mu.Lock()
	defer g.mu.Unlock()
	return MigrationTaskStats{Active: g.active}
}
