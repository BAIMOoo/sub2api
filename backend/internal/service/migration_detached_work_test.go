package service

import (
	"context"
	"errors"
	"testing"
	"time"

	"golang.org/x/sync/singleflight"
)

func enableMigrationDetachedTest(t *testing.T) {
	t.Helper()
	t.Setenv("MIGRATION_SHARED_REDIS", "true")
	if stats := MigrationDetachedTaskStats(); stats.Active != 0 || stats.Pending != 0 {
		t.Fatalf("earlier test left detached work: %+v %v", stats, MigrationDetachedTaskSites())
	}
}

func requireMigrationDetachedActive(t *testing.T, want uint64) {
	t.Helper()
	if stats := MigrationDetachedTaskStats(); stats.Active != want || stats.Pending != 0 {
		t.Fatalf("detached work = %+v, want active=%d", stats, want)
	}
}

func TestMigrationDetachedAdmissionAndNestedWorkHaveNoIdleGap(t *testing.T) {
	enableMigrationDetachedTest(t)
	done := BeginMigrationDetachedTask("parent")
	// Registration is visible before a goroutine has even been launched.
	requireMigrationDetachedActive(t, 1)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := WaitForMigrationDetachedTasks(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("idle falsely proven before launch: %v", err)
	}
	nested := BeginMigrationDetachedTask("nested")
	done()
	done()
	requireMigrationDetachedActive(t, 1)
	if sites := MigrationDetachedTaskSites(); sites["nested"] != 1 || sites["parent"] != 0 {
		t.Fatalf("sites = %v", sites)
	}
	nested()
	if err := WaitForMigrationDetachedTasks(context.Background()); err != nil {
		t.Fatal(err)
	}
	requireMigrationDetachedActive(t, 0)
}

func TestMigrationDetachedSingleflightOutlivesCanceledAndIgnoredCallers(t *testing.T) {
	enableMigrationDetachedTest(t)
	var group singleflight.Group
	entered, release := make(chan struct{}), make(chan struct{})
	first := MigrationSingleflightDoChan("first", &group, "key", func() (any, error) {
		close(entered)
		<-release
		return "persisted", nil
	})
	<-entered
	// This follower intentionally ignores its result, as the canceled request
	// would. Its accounting must still complete when the shared callback does.
	_ = MigrationSingleflightDoChan("ignored follower", &group, "key", func() (any, error) {
		t.Error("singleflight ran follower callback")
		return nil, nil
	})
	requireMigrationDetachedActive(t, 2)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := WaitForMigrationDetachedTasks(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("shared work vanished after cancellation: %v", err)
	}
	close(release)
	result := <-first
	if result.Err != nil || result.Val != "persisted" || !result.Shared {
		t.Fatalf("singleflight result changed: %+v", result)
	}
	ctx, cancel = context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := WaitForMigrationDetachedTasks(ctx); err != nil {
		t.Fatal(err)
	}
	requireMigrationDetachedActive(t, 0)
}

func TestMigrationDetachedQueueRejectRollsBackAndBatchLifetimePersists(t *testing.T) {
	enableMigrationDetachedTest(t)
	queue := &SubscriptionMaintenanceQueue{queue: make(chan func(), 1)}
	entered, release := make(chan struct{}), make(chan struct{})
	if err := queue.TryEnqueue(func() { close(entered); <-release }); err != nil {
		t.Fatal(err)
	}
	if err := queue.TryEnqueue(func() {}); err == nil {
		t.Fatal("full queue accepted")
	}
	requireMigrationDetachedActive(t, 1)
	work := <-queue.queue
	go work()
	<-entered
	// An empty channel does not prove that dequeued work was persisted.
	requireMigrationDetachedActive(t, 1)
	close(release)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := WaitForMigrationDetachedTasks(ctx); err != nil {
		t.Fatal(err)
	}
}

func TestMigrationDetachedModerationDrainsBeforeWorkersAreFenced(t *testing.T) {
	enableMigrationDetachedTest(t)
	svc := &ContentModerationService{
		settingRepo: &opsRuntimeRefreshRepo{},
		asyncQueue:  make(chan contentModerationTask, 1),
	}
	svc.runtimeSnapshot.Store(&contentModerationRuntimeSnapshot{
		loadedAt: time.Now(), config: &ContentModerationConfig{WorkerCount: 1},
	})
	svc.enqueueAsync(ContentModerationCheckInput{}, nil, ContentModerationInput{}, "hash")
	requireMigrationDetachedActive(t, 1)
	result := make(chan error, 1)
	resumes := make(chan func(), 1)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	go func() {
		resume, err := svc.BeginMigrationQuiet(ctx)
		resumes <- resume
		result <- err
	}()
	// Existing workers must still enter while accepted decisions are queued.
	release, ok := svc.migrationWorkGate.TryEnter()
	if !ok {
		t.Fatal("moderation workers fenced before queue drain")
	}
	svc.workerOnce(0)
	release()
	if err := <-result; err != nil {
		t.Fatal(err)
	}
	resume := <-resumes
	if _, ok := svc.migrationWorkGate.TryEnter(); ok {
		t.Fatal("idle moderation producer not fenced")
	}
	if stats := svc.MigrationTaskStats(); stats.Active != 0 || stats.Pending != 0 {
		t.Fatalf("moderation not quiet: %+v", stats)
	}
	resume()
	requireMigrationDetachedActive(t, 0)
}

func TestMigrationDetachedAuthOutboxWaitsChildrenAndResumes(t *testing.T) {
	entered, release := make(chan struct{}), make(chan struct{})
	repo := &authInvalidationRepoStub{events: []AuthCacheInvalidationEvent{{ID: 1, Stage: 1}}}
	cache := &authInvalidationCacheStub{deleteFn: func(context.Context, string) error {
		close(entered)
		<-release
		return nil
	}}
	worker := NewAuthCacheInvalidationWorker(repo, cache)
	batchDone := make(chan error, 1)
	go func() { batchDone <- worker.processBatch(context.Background()) }()
	<-entered
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	resume, err := worker.BeginMigrationQuiet(ctx)
	if !errors.Is(err, context.Canceled) || worker.MigrationTaskStats().Active != 1 {
		t.Fatalf("active outbox child not held: %+v %v", worker.MigrationTaskStats(), err)
	}
	close(release)
	if err := <-batchDone; err != nil {
		t.Fatal(err)
	}
	repo.mu.Lock()
	repo.claimLimit = 0
	repo.mu.Unlock()
	if err := worker.processBatch(context.Background()); err != nil {
		t.Fatal(err)
	}
	if repo.claimLimit != 0 {
		t.Fatal("paused outbox claimed durable work")
	}
	resume()
	cache.deleteFn = nil
	if err := worker.processBatch(context.Background()); err != nil {
		t.Fatal(err)
	}
	if repo.claimLimit == 0 {
		t.Fatal("resumed outbox did not claim")
	}
}

func TestMigrationDetachedOpsRuntimeRefreshIsReversiblyFenced(t *testing.T) {
	repo := &opsRuntimeRefreshRepo{values: map[string]string{}}
	svc := &OpsService{settingRepo: repo}
	svc.startRuntimeSettingsRefresh(context.Background(), 2*time.Millisecond, 0, time.Second)
	defer svc.StopRuntimeSettingsRefresh()
	waitForOpsRefresh(t, time.Second, func() bool { return repo.calls.Load() > 0 })
	resume, err := svc.BeginMigrationRuntimeQuiet(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	calls := repo.calls.Load()
	time.Sleep(20 * time.Millisecond)
	if repo.calls.Load() != calls || svc.MigrationRuntimeTaskStats().Active != 0 {
		t.Fatal("paused ops runtime read database")
	}
	resume()
	waitForOpsRefresh(t, time.Second, func() bool { return repo.calls.Load() > calls })
}
