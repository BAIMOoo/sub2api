package service

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
)

func TestMigrationUsagePoolWaitIncludesInlineFallback(t *testing.T) {
	pool := NewUsageRecordWorkerPoolWithOptions(UsageRecordWorkerPoolOptions{WorkerCount: 1, QueueSize: 1, TaskTimeout: time.Minute, OverflowPolicy: config.UsageRecordOverflowPolicySync})
	defer pool.Stop()
	workerStarted, releaseWorker := make(chan struct{}), make(chan struct{})
	inlineStarted, releaseInline := make(chan struct{}), make(chan struct{})
	pool.Submit(func(context.Context) { close(workerStarted); <-releaseWorker })
	<-workerStarted
	pool.Submit(func(context.Context) {})
	inlineDone := make(chan UsageRecordSubmitMode, 1)
	go func() { inlineDone <- pool.Submit(func(context.Context) { close(inlineStarted); <-releaseInline }) }()
	<-inlineStarted
	if stats := pool.MigrationTaskStats(); stats.Active != 2 || stats.Pending != 1 {
		t.Fatalf("missing queued/inline work: %+v", stats)
	}
	close(releaseWorker)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	if err := pool.WaitForIdle(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("inline fallback not awaited: %v", err)
	}
	stopDone := make(chan struct{})
	go func() { pool.Stop(); close(stopDone) }()
	select {
	case <-stopDone:
		t.Fatal("Stop omitted inline fallback")
	case <-time.After(10 * time.Millisecond):
	}
	close(releaseInline)
	if mode := <-inlineDone; mode != UsageRecordSubmitModeSync {
		t.Fatalf("mode: %v", mode)
	}
	select {
	case <-stopDone:
	case <-time.After(time.Second):
		t.Fatal("stop did not finish")
	}
}

type migrationBillingCache struct {
	BillingCache
	started chan struct{}
	release chan struct{}
}

func (cache migrationBillingCache) SetUserBalance(context.Context, int64, float64) error {
	close(cache.started)
	<-cache.release
	return nil
}

func TestMigrationBillingCacheWaitIncludesDequeuedTask(t *testing.T) {
	started, release := make(chan struct{}), make(chan struct{})
	s := &BillingCacheService{cache: migrationBillingCache{started: started, release: release}}
	s.startCacheWriteWorkers()
	defer s.Stop()
	if !s.enqueueCacheWrite(cacheWriteTask{kind: cacheWriteSetBalance, userID: 1}) {
		t.Fatal("not queued")
	}
	<-started
	if len(s.cacheWriteChan) != 0 {
		t.Fatal("task did not leave queue")
	}
	if stats := s.MigrationTaskStats(); stats.Active != 1 {
		t.Fatalf("dequeued task invisible: %+v", stats)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	if err := s.WaitForIdle(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("active task not awaited: %v", err)
	}
	close(release)
	if err := s.WaitForIdle(context.Background()); err != nil {
		t.Fatal(err)
	}
}

type migrationQuietAuditRepo struct {
	AuditLogRepository
	calls atomic.Uint64
	fail  bool
}

func (repo *migrationQuietAuditRepo) BatchInsert(context.Context, []*AuditLog) (int64, error) {
	repo.calls.Add(1)
	if repo.fail {
		return 0, errors.New("audit unavailable")
	}
	return 1, nil
}

func TestMigrationAuditFlushLocalBatchAndResume(t *testing.T) {
	repo := &migrationQuietAuditRepo{}
	s := NewAuditLogService(repo, nil)
	s.wg.Add(1)
	go s.runWriter()
	defer s.Stop()
	s.Record(&AuditLog{})
	resume, err := s.PauseAndFlush(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if repo.calls.Load() != 1 {
		t.Fatal("local batch not flushed")
	}
	if stats := s.MigrationTaskStats(); stats.Active != 0 || stats.Pending != 0 {
		t.Fatalf("not quiet: %+v", stats)
	}
	s.Record(&AuditLog{})
	if repo.calls.Load() != 1 {
		t.Fatal("paused writer persisted new work")
	}
	if stats := s.MigrationTaskStats(); stats.Pending != 0 || stats.Deferred != 1 {
		t.Fatalf("paused admission not retained: %+v", stats)
	}
	resume()
	resume()
	resume, err = s.PauseAndFlush(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	resume()
	if repo.calls.Load() != 2 {
		t.Fatal("resumed writer lost accepted work")
	}
}

func TestMigrationAuditFailedBatchBlocksQuietDespiteEmptyQueue(t *testing.T) {
	repo := &migrationQuietAuditRepo{fail: true}
	s := NewAuditLogService(repo, nil)
	s.wg.Add(1)
	go s.runWriter()
	defer s.Stop()
	s.Record(&AuditLog{})
	if _, err := s.PauseAndFlush(context.Background()); err == nil {
		t.Fatal("failed accepted batch became quiet")
	}
	if stats := s.MigrationTaskStats(); stats.Failed == 0 {
		t.Fatal("write failure invisible")
	}
}

func TestMigrationSystemLogFlushAndResume(t *testing.T) {
	var calls atomic.Uint64
	repo := &opsRepoMock{BatchInsertSystemLogsFn: func(context.Context, []*OpsInsertSystemLogInput) (int64, error) { calls.Add(1); return 1, nil }}
	s := NewOpsSystemLogSink(repo)
	s.flushInterval = time.Hour
	s.Start()
	defer s.Stop()
	s.WriteLogEvent(&logger.LogEvent{Level: "warn", Message: "before"})
	resume, err := s.PauseAndFlush(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 1 {
		t.Fatal("batch not flushed")
	}
	s.WriteLogEvent(&logger.LogEvent{Level: "warn", Message: "after"})
	if stats := s.MigrationTaskStats(); stats.Pending != 0 || stats.Active != 0 || stats.Deferred != 1 {
		t.Fatalf("handover warning invalidated the committed writer fence: %+v", stats)
	}
	if calls.Load() != 1 {
		t.Fatal("paused writer persisted new work")
	}
	resume()
	resume, err = s.PauseAndFlush(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	resume()
	if calls.Load() != 2 {
		t.Fatal("resumed writer lost log")
	}
}

func TestMigrationWriterQuietRetainsHandoverWarningsUntilResume(t *testing.T) {
	prepareMigrationBackgroundActivation(t)
	var calls atomic.Uint64
	repo := &opsRepoMock{BatchInsertSystemLogsFn: func(context.Context, []*OpsInsertSystemLogInput) (int64, error) {
		calls.Add(1)
		return 1, nil
	}}
	s := NewOpsSystemLogSink(repo)
	s.flushInterval = time.Hour
	s.Start()
	defer s.Stop()
	migrationWriterRegistry.Lock()
	migrationWriterRegistry.bindings["ops_system_log"] = migrationWriterBinding{s.MigrationTaskStats, s.PauseAndFlush}
	migrationWriterRegistry.Unlock()
	lease, err := BeginMigrationWriterQuiesce(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Resume()
	// This can originate in an idle upstream ping while both data pools are
	// fenced. It must neither write there nor prevent authority-aware recovery.
	s.WriteLogEvent(&logger.LogEvent{Level: "warn", Message: "upstream ping failed during data handover"})
	if err := MigrationWritersQuiet(); err != nil {
		t.Fatalf("handover warning deadlocked the committed quiet phase: %v", err)
	}
	if calls.Load() != 0 || s.MigrationTaskStats().Deferred != 1 {
		t.Fatal("post-fence observation was lost or sent to the old authority")
	}
	lease.Resume()
	resume, err := s.PauseAndFlush(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	resume()
	if calls.Load() != 1 || s.MigrationTaskStats().Deferred != 0 {
		t.Fatal("resume did not persist the retained observation exactly once")
	}
}

func TestMigrationIngressRejectFlushRetainsRetryAndResumes(t *testing.T) {
	repo := &ingressRejectRepoStub{failCount: 1}
	s := NewOpsIngressRejectAggregator(repo)
	s.Start()
	defer s.Stop()
	s.RecordIngressReject("auth", "responses", "sse", "192.0.2.1", 0, 0)
	if _, err := s.PauseAndFlush(context.Background()); err == nil {
		t.Fatal("DB failure swallowed")
	}
	if !s.Health().Accepting {
		t.Fatal("failed pause did not resume producer")
	}
	resume, err := s.PauseAndFlush(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if stats := s.MigrationTaskStats(); stats.Active != 0 || stats.Pending != 0 {
		t.Fatalf("not quiet: %+v", stats)
	}
	resume()
	if !s.Health().Accepting {
		t.Fatal("producer stayed paused")
	}
	repo.mu.Lock()
	defer repo.mu.Unlock()
	if repo.requests != 1 {
		t.Fatalf("accepted aggregate lost or duplicated: %d", repo.requests)
	}
}
