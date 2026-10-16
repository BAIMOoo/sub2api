package service

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/zeromicro/go-zero/core/collection"
)

var newTimingWheel = collection.NewTimingWheel

// TimingWheelService wraps go-zero's TimingWheel for task scheduling
type TimingWheelService struct {
	tw       *collection.TimingWheel
	stopOnce sync.Once
	mu       sync.Mutex
	stopped  bool
	pauses   int
	active   uint64
	changed  chan struct{}
	tasks    map[string]*timingWheelServiceTask
}

type timingWheelServiceTask struct {
	name      string
	fn        func()
	interval  time.Duration
	recurring bool
	canceled  bool
	active    uint64 // protected by TimingWheelService.mu
}

// NewTimingWheelService creates a new TimingWheelService instance
func NewTimingWheelService() (*TimingWheelService, error) {
	// 1 second tick, 3600 slots = supports up to 1 hour delay
	// execute function: runs func() type tasks
	tw, err := newTimingWheel(1*time.Second, 3600, func(key, value any) {
		if fn, ok := value.(func()); ok {
			fn()
		}
	})
	if err != nil {
		return nil, fmt.Errorf("创建 timing wheel 失败: %w", err)
	}
	return &TimingWheelService{tw: tw, changed: make(chan struct{}), tasks: make(map[string]*timingWheelServiceTask)}, nil
}

// Start starts the timing wheel
func (s *TimingWheelService) Start() {
	logger.LegacyPrintf("service.timing_wheel", "%s", "[TimingWheel] Started (auto-start by go-zero)")
}

// Stop stops the timing wheel
func (s *TimingWheelService) Stop() {
	_ = s.StopAndWait(context.Background())
}

func (s *TimingWheelService) StopAndWait(ctx context.Context) error {
	s.stopOnce.Do(func() {
		s.mu.Lock()
		s.stopped = true
		for _, task := range s.tasks {
			task.canceled = true
		}
		s.tasks = make(map[string]*timingWheelServiceTask)
		s.notifyLocked()
		s.tw.Stop()
		s.mu.Unlock()
		logger.LegacyPrintf("service.timing_wheel", "%s", "[TimingWheel] Stopped")
	})
	return s.WaitForIdle(ctx)
}

// Schedule schedules a one-time task
func (s *TimingWheelService) Schedule(name string, delay time.Duration, fn func()) {
	s.schedule(&timingWheelServiceTask{name: name, fn: fn, interval: delay})
}

// ScheduleRecurring schedules a recurring task
func (s *TimingWheelService) ScheduleRecurring(name string, interval time.Duration, fn func()) {
	s.schedule(&timingWheelServiceTask{name: name, fn: fn, interval: interval, recurring: true})
}

// Cancel cancels a scheduled task
func (s *TimingWheelService) Cancel(name string) {
	s.mu.Lock()
	if task := s.tasks[name]; task != nil {
		task.canceled = true
		delete(s.tasks, name)
	}
	_ = s.tw.RemoveTimer(name)
	s.notifyLocked()
	s.mu.Unlock()
}

func (s *TimingWheelService) notifyLocked() {
	if s.changed != nil {
		close(s.changed)
	}
	s.changed = make(chan struct{})
}

func (s *TimingWheelService) schedule(task *timingWheelServiceTask) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.stopped {
		logger.LegacyPrintf("service.timing_wheel", "[TimingWheel] SetTimer failed for %q: %v", task.name, collection.ErrClosed)
		return
	}
	if old := s.tasks[task.name]; old != nil {
		old.canceled = true
	}
	s.tasks[task.name] = task
	if err := s.setTimerLocked(task); err != nil {
		delete(s.tasks, task.name)
		task.canceled = true
		logger.LegacyPrintf("service.timing_wheel", "[TimingWheel] SetTimer failed for %q: %v", task.name, err)
	}
}

func (s *TimingWheelService) setTimerLocked(task *timingWheelServiceTask) error {
	return s.tw.SetTimer(task.name, func() { s.executeTask(task) }, task.interval)
}

func (s *TimingWheelService) executeTask(task *timingWheelServiceTask) {
	for {
		s.mu.Lock()
		if s.stopped || task.canceled || s.tasks[task.name] != task {
			s.mu.Unlock()
			return
		}
		if s.pauses == 0 {
			s.active++
			task.active++
			s.notifyLocked()
			s.mu.Unlock()
			break
		}
		changed := s.changed
		s.mu.Unlock()
		<-changed // Fenced future callback; never admitted as a writer.
	}
	defer func() {
		s.mu.Lock()
		defer s.mu.Unlock()
		s.active--
		task.active--
		if !s.stopped && !task.canceled && s.tasks[task.name] == task {
			if task.recurring {
				if err := s.setTimerLocked(task); err != nil {
					logger.LegacyPrintf("service.timing_wheel", "[TimingWheel] recurring SetTimer failed for %q: %v", task.name, err)
				}
			} else {
				delete(s.tasks, task.name)
			}
		}
		s.notifyLocked()
	}()
	if task.fn != nil {
		task.fn()
	}
}

// MigrationTaskStats counts callbacks actually admitted to execute, not idle
// future timers. PauseAndWait keeps those future timers behind a durable fence.
func (s *TimingWheelService) MigrationTaskStats() MigrationTaskStats {
	s.mu.Lock()
	defer s.mu.Unlock()
	return MigrationTaskStats{Active: s.active}
}

func (s *TimingWheelService) WaitForIdle(ctx context.Context) error {
	return s.wait(ctx, nil)
}

func (s *TimingWheelService) wait(ctx context.Context, task *timingWheelServiceTask) error {
	for {
		s.mu.Lock()
		active := s.active
		if task != nil {
			active = task.active
		}
		changed := s.changed
		s.mu.Unlock()
		if err := ctx.Err(); err != nil {
			return err
		}
		if active == 0 {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-changed:
		}
	}
}

// PauseAndWait fences callback admission without canceling or losing future
// timers. A timeout automatically releases only this caller's pause lease.
func (s *TimingWheelService) PauseAndWait(ctx context.Context) (func(), error) {
	s.mu.Lock()
	s.pauses++
	s.notifyLocked()
	s.mu.Unlock()
	var once sync.Once
	resume := func() {
		once.Do(func() {
			s.mu.Lock()
			s.pauses--
			s.notifyLocked()
			s.mu.Unlock()
		})
	}
	if err := s.WaitForIdle(ctx); err != nil {
		resume()
		return nil, err
	}
	return resume, nil
}

// CancelAndWait invalidates a recurring callback's identity before waiting.
// An already executing old callback cannot re-arm itself after cancellation.
func (s *TimingWheelService) CancelAndWait(ctx context.Context, name string) error {
	s.mu.Lock()
	task := s.tasks[name]
	if task != nil {
		task.canceled = true
		delete(s.tasks, name)
	}
	_ = s.tw.RemoveTimer(name)
	s.notifyLocked()
	s.mu.Unlock()
	if task == nil {
		return nil
	}
	return s.wait(ctx, task)
}
