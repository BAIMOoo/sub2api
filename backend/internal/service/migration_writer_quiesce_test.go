package service

import (
	"context"
	"errors"
	"reflect"
	"sync"
	"testing"
	"time"
)

func resetMigrationWriters(t *testing.T) {
	t.Helper()
	migrationWriterRegistry.Lock()
	previous := migrationWriterRegistry.bindings
	leased := migrationWriterRegistry.leased
	quiet := migrationWriterRegistry.quiet
	migrationWriterRegistry.bindings = make(map[string]migrationWriterBinding)
	migrationWriterRegistry.leased = false
	migrationWriterRegistry.quiet = false
	migrationWriterRegistry.Unlock()
	t.Cleanup(func() {
		migrationWriterRegistry.Lock()
		defer migrationWriterRegistry.Unlock()
		migrationWriterRegistry.bindings = previous
		migrationWriterRegistry.leased = leased
		migrationWriterRegistry.quiet = quiet
	})
}

func TestMigrationWriterCoverageAndReverseRollback(t *testing.T) {
	resetMigrationWriters(t)
	if _, err := BeginMigrationWriterQuiesce(context.Background()); err == nil {
		t.Fatal("missing writer coverage accepted")
	}
	var resumed []string
	for _, name := range MigrationRequiredWriterNames() {
		name := name
		RegisterMigrationWriterQuiescer(name, func() MigrationTaskStats { return MigrationTaskStats{} }, func(context.Context) (func(), error) {
			resume := func() { resumed = append(resumed, name) }
			if name == "plugins" {
				return resume, errors.New("busy")
			}
			return resume, nil
		})
	}
	if _, err := BeginMigrationWriterQuiesce(context.Background()); err == nil {
		t.Fatal("busy writer accepted")
	}
	if !reflect.DeepEqual(resumed, []string{"plugins", "scheduled_writers", "timing_wheel"}) {
		t.Fatalf("rollback order: %v", resumed)
	}
	if migrationWriterRegistry.leased {
		t.Fatal("failed lease remained held")
	}
}

func TestMigrationWriterLeaseExclusiveIdempotentAndProbeFailure(t *testing.T) {
	resetMigrationWriters(t)
	var resumes int
	var pending uint64
	for _, name := range MigrationRequiredWriterNames() {
		RegisterMigrationWriterQuiescer(name, func() MigrationTaskStats { return MigrationTaskStats{Pending: pending} }, func(context.Context) (func(), error) {
			return func() { resumes++ }, nil
		})
	}
	lease, err := BeginMigrationWriterQuiesce(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := BeginMigrationWriterQuiesce(context.Background()); err == nil {
		t.Fatal("second lease accepted")
	}
	if resumes != 0 {
		t.Fatal("fences prematurely released")
	}
	lease.Resume()
	lease.Resume()
	if resumes != len(migrationRequiredWriters) {
		t.Fatalf("resumes: %d", resumes)
	}
	pending = 1
	if _, err := BeginMigrationWriterQuiesce(context.Background()); err == nil {
		t.Fatal("pending writer accepted")
	}
	if migrationWriterRegistry.leased {
		t.Fatal("probe failure left lease held")
	}
}

func TestMigrationWriterPanicsRollbackPreviousFences(t *testing.T) {
	resetMigrationWriters(t)
	var resumed []string
	for _, name := range MigrationRequiredWriterNames() {
		name := name
		RegisterMigrationWriterQuiescer(name, func() MigrationTaskStats { return MigrationTaskStats{} }, func(context.Context) (func(), error) {
			if name == "plugins" {
				panic("unexpected")
			}
			return func() { resumed = append(resumed, name) }, nil
		})
	}
	if _, err := BeginMigrationWriterQuiesce(context.Background()); err == nil {
		t.Fatal("panic became success")
	}
	if !reflect.DeepEqual(resumed, []string{"scheduled_writers", "timing_wheel"}) {
		t.Fatalf("rollback order: %v", resumed)
	}
}

func TestMigrationBufferedWriterTimeoutReleasesLatePause(t *testing.T) {
	var writer migrationBufferedWriter
	writer.init()
	ctx, cancel := context.WithCancel(context.Background())
	received := make(chan migrationFlushRequest)
	go func() { request := <-writer.requests; received <- request }()
	done := make(chan error, 1)
	go func() { _, err := writer.pauseAndFlush(ctx, nil); done <- err }()
	request := <-received
	cancel()
	if !errors.Is(<-done, context.Canceled) {
		t.Fatal("cancellation not returned")
	}
	select {
	case <-request.release:
	case <-time.After(time.Second):
		t.Fatal("late writer would stay paused")
	}
}

type migrationDeferredRepo struct {
	AccountRepository
	batch func(context.Context, map[int64]time.Time) error
}

func (r migrationDeferredRepo) BatchUpdateLastUsed(ctx context.Context, updates map[int64]time.Time) error {
	return r.batch(ctx, updates)
}

func TestMigrationDeferredConcurrentNewerUpdateSurvivesFailure(t *testing.T) {
	started, release := make(chan struct{}), make(chan struct{})
	var mu sync.Mutex
	var calls []time.Time
	var service *DeferredService
	repo := migrationDeferredRepo{batch: func(_ context.Context, updates map[int64]time.Time) error {
		mu.Lock()
		calls = append(calls, updates[7])
		count := len(calls)
		mu.Unlock()
		if count == 1 {
			close(started)
			<-release
			return errors.New("retry")
		}
		return nil
	}}
	service = NewDeferredService(repo, nil, time.Second)
	old, newer := time.Now().Add(-time.Hour), time.Now()
	service.lastUsedUpdates.Store(int64(7), old)
	done := make(chan error, 1)
	go func() { done <- service.FlushAndWait(context.Background()) }()
	<-started
	service.lastUsedMu.Lock()
	service.lastUsedUpdates.Store(int64(7), newer)
	service.lastUsedMu.Unlock()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := service.FlushAndWait(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("waiting cancellation: %v", err)
	}
	close(release)
	if <-done == nil {
		t.Fatal("write failure swallowed")
	}
	if err := service.FlushAndWait(context.Background()); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(calls) != 2 || !calls[1].Equal(newer) {
		t.Fatalf("newer update overwritten: %v", calls)
	}
	if stats := service.MigrationTaskStats(); stats.Active != 0 || stats.Pending != 0 {
		t.Fatalf("not idle: %+v", stats)
	}
}

func TestMigrationTimingWheelPauseCancelAndStopWait(t *testing.T) {
	s, err := NewTimingWheelService()
	if err != nil {
		t.Fatal(err)
	}
	defer s.Stop()
	entered, release := make(chan struct{}), make(chan struct{})
	s.ScheduleRecurring("running", time.Hour, func() { close(entered); <-release })
	s.mu.Lock()
	task := s.tasks["running"]
	s.mu.Unlock()
	go s.executeTask(task)
	<-entered
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := s.PauseAndWait(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("pause cancellation: %v", err)
	}
	stopped := make(chan struct{})
	go func() { s.Stop(); close(stopped) }()
	select {
	case <-stopped:
		t.Fatal("stop returned with callback active")
	case <-time.After(10 * time.Millisecond):
	}
	close(release)
	select {
	case <-stopped:
	case <-time.After(time.Second):
		t.Fatal("stop did not finish")
	}
	if stats := s.MigrationTaskStats(); stats.Active != 0 {
		t.Fatalf("active: %+v", stats)
	}
}

func TestMigrationTimingWheelPauseRetainsFutureCallback(t *testing.T) {
	s, err := NewTimingWheelService()
	if err != nil {
		t.Fatal(err)
	}
	defer s.Stop()
	run := make(chan struct{})
	s.Schedule("future", time.Hour, func() { close(run) })
	s.mu.Lock()
	task := s.tasks["future"]
	s.mu.Unlock()
	resume, err := s.PauseAndWait(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	go s.executeTask(task)
	select {
	case <-run:
		t.Fatal("callback entered fenced writer")
	case <-time.After(10 * time.Millisecond):
	}
	resume()
	resume()
	select {
	case <-run:
	case <-time.After(time.Second):
		t.Fatal("callback was lost")
	}
}
