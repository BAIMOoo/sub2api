package service

import (
	"context"
	"errors"
	"log"
	"sync"
	"time"
)

// DeferredService provides deferred batch update functionality
type DeferredService struct {
	accountRepo AccountRepository
	timingWheel *TimingWheelService
	interval    time.Duration

	lastUsedUpdates sync.Map
	lastUsedMu      sync.Mutex
	flushInit       sync.Once
	flushToken      chan struct{}
	flushing        bool
	flushFailures   uint64
}

// NewDeferredService creates a new DeferredService instance
func NewDeferredService(accountRepo AccountRepository, timingWheel *TimingWheelService, interval time.Duration) *DeferredService {
	return &DeferredService{
		accountRepo: accountRepo,
		timingWheel: timingWheel,
		interval:    interval,
	}
}

// Start starts the deferred service
func (s *DeferredService) Start() {
	s.timingWheel.ScheduleRecurring("deferred:last_used", s.interval, s.flushLastUsed)
	log.Printf("[DeferredService] Started (interval: %v)", s.interval)
}

// Stop stops the deferred service
func (s *DeferredService) Stop() {
	if s.timingWheel != nil {
		_ = s.timingWheel.CancelAndWait(context.Background(), "deferred:last_used")
	}
	s.flushLastUsed()
	log.Printf("[DeferredService] Service stopped")
}

func (s *DeferredService) ScheduleLastUsedUpdate(accountID int64) {
	s.lastUsedMu.Lock()
	defer s.lastUsedMu.Unlock()
	s.lastUsedUpdates.Store(accountID, time.Now())
}

func (s *DeferredService) flushLastUsed() {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := s.FlushAndWait(ctx); err != nil {
		log.Printf("[DeferredService] BatchUpdateLastUsed flush failed: %v", err)
	}
}

// FlushAndWait serializes with an already executing periodic flush, persists
// pending last_used updates, and leaves the service reusable after a quiet
// migration window. Errors retain the newest timestamp for every account.
// The caller must fence producers to keep an empty queue empty after return.
func (s *DeferredService) FlushAndWait(ctx context.Context) error {
	s.flushInit.Do(func() {
		s.flushToken = make(chan struct{}, 1)
		s.flushToken <- struct{}{}
	})
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-s.flushToken:
	}
	defer func() { s.flushToken <- struct{}{} }()
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		if err := s.flushLastUsedBatch(ctx); err != nil {
			return err
		}
		if s.MigrationTaskStats().Pending == 0 {
			return nil
		}
	}
}

func (s *DeferredService) flushLastUsedBatch(ctx context.Context) error {
	updates := make(map[int64]time.Time)
	s.lastUsedMu.Lock()
	s.lastUsedUpdates.Range(func(key, value any) bool {
		id, ok := key.(int64)
		if !ok {
			return true
		}
		ts, ok := value.(time.Time)
		if !ok {
			return true
		}
		updates[id] = ts
		s.lastUsedUpdates.Delete(key)
		return true
	})

	if len(updates) == 0 {
		s.lastUsedMu.Unlock()
		return nil
	}
	s.flushing = true
	s.lastUsedMu.Unlock()
	var err error
	if s.accountRepo == nil {
		err = errors.New("deferred last_used repository is unavailable")
	} else {
		err = s.accountRepo.BatchUpdateLastUsed(ctx, updates)
	}
	s.lastUsedMu.Lock()
	defer s.lastUsedMu.Unlock()
	s.flushing = false
	if err != nil {
		s.flushFailures++
		log.Printf("[DeferredService] BatchUpdateLastUsed failed (%d accounts): %v", len(updates), err)
		for id, ts := range updates {
			if value, exists := s.lastUsedUpdates.Load(id); exists {
				if newer, ok := value.(time.Time); ok && newer.After(ts) {
					continue
				}
			}
			s.lastUsedUpdates.Store(id, ts)
		}
	} else {
		log.Printf("[DeferredService] BatchUpdateLastUsed flushed %d accounts", len(updates))
	}
	return err
}

func (s *DeferredService) MigrationTaskStats() MigrationTaskStats {
	s.lastUsedMu.Lock()
	defer s.lastUsedMu.Unlock()
	stats := MigrationTaskStats{Failed: s.flushFailures}
	if s.flushing {
		stats.Active = 1
	}
	s.lastUsedUpdates.Range(func(_, _ any) bool { stats.Pending++; return true })
	return stats
}
