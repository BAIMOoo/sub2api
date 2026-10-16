package service

import (
	"context"
	"reflect"
	"sync/atomic"
	"testing"
)

func resetMigrationBackgroundWorkers(t *testing.T) {
	t.Helper()
	migrationStartedBackgroundWorkers.Lock()
	names, deferred, failures := migrationStartedBackgroundWorkers.names, migrationStartedBackgroundWorkers.deferred, migrationStartedBackgroundWorkers.failures
	activated, activating := migrationStartedBackgroundWorkers.activated, migrationStartedBackgroundWorkers.activating
	migrationStartedBackgroundWorkers.names = make(map[string]bool)
	migrationStartedBackgroundWorkers.deferred = make(map[string]func())
	migrationStartedBackgroundWorkers.failures = make(map[string]string)
	migrationStartedBackgroundWorkers.activated, migrationStartedBackgroundWorkers.activating = false, false
	migrationStartedBackgroundWorkers.Unlock()
	t.Cleanup(func() {
		migrationStartedBackgroundWorkers.Lock()
		defer migrationStartedBackgroundWorkers.Unlock()
		migrationStartedBackgroundWorkers.names, migrationStartedBackgroundWorkers.deferred, migrationStartedBackgroundWorkers.failures = names, deferred, failures
		migrationStartedBackgroundWorkers.activated, migrationStartedBackgroundWorkers.activating = activated, activating
	})
}

func prepareMigrationBackgroundActivation(t *testing.T) {
	t.Helper()
	resetMigrationWriters(t)
	resetMigrationBackgroundWorkers(t)
	t.Setenv("MIGRATION_SHARED_REDIS", "true")
	t.Setenv("MIGRATION_BACKGROUND_WORKERS", "false")
	for _, name := range MigrationRequiredWriterNames() {
		begin := func(context.Context) (func(), error) { return func() {}, nil }
		if name == "scheduled_writers" {
			begin = func(context.Context) (func(), error) { return func() {}, MigrationScheduledWritersQuiet() }
		}
		RegisterMigrationWriterQuiescer(name, func() MigrationTaskStats { return MigrationTaskStats{} }, begin)
	}
}

func TestMigrationBackgroundActivationIsOnceOnlyAndPermanentlyFencesQuiet(t *testing.T) {
	prepareMigrationBackgroundActivation(t)
	var starts []string
	startMigrationBackgroundWorker("B", func() { starts = append(starts, "B") })
	startMigrationBackgroundWorker("A", func() { starts = append(starts, "A") })
	if len(starts) != 0 {
		t.Fatal("replica started producers before activation")
	}
	lease, err := BeginMigrationWriterQuiesce(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if err := ActivateMigrationBackgroundWorkers(); err == nil {
		t.Fatal("activated while data handover lease held")
	}
	if MigrationBackgroundActivationStatus().Activated {
		t.Fatal("rejected activation changed status")
	}
	lease.Resume()
	if err := ActivateMigrationBackgroundWorkers(); err != nil {
		t.Fatal(err)
	}
	if err := ActivateMigrationBackgroundWorkers(); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(starts, []string{"A", "B"}) {
		t.Fatalf("starts = %v", starts)
	}
	status := MigrationBackgroundActivationStatus()
	if !status.Activated || status.Activating || len(status.Pending) != 0 || !reflect.DeepEqual(status.Started, starts) {
		t.Fatalf("status = %+v", status)
	}
	if _, err := BeginMigrationWriterQuiesce(context.Background()); err == nil {
		t.Fatal("quiet handover accepted after producers activated")
	}
	// Changing configuration cannot manufacture the original producer fence.
	t.Setenv("MIGRATION_BACKGROUND_WORKERS", "false")
	if !MigrationBackgroundWorkersEnabled() || MigrationScheduledWritersQuiet() == nil {
		t.Fatal("activation was reversible by environment")
	}
}

func TestMigrationBackgroundActivationPanicIsNotRetried(t *testing.T) {
	prepareMigrationBackgroundActivation(t)
	var attempted int
	var later int
	startMigrationBackgroundWorker("A", func() { attempted++; panic("partial start") })
	startMigrationBackgroundWorker("B", func() { later++ })
	if err := ActivateMigrationBackgroundWorkers(); err == nil {
		t.Fatal("panic accepted")
	}
	status := MigrationBackgroundActivationStatus()
	if status.Activating || !status.Activated || len(status.Failures) != 1 || !reflect.DeepEqual(status.Pending, []string{"B"}) {
		t.Fatalf("status = %+v", status)
	}
	if err := ActivateMigrationBackgroundWorkers(); err == nil {
		t.Fatal("prior partial failure forgotten")
	}
	if attempted != 1 || later != 1 {
		t.Fatalf("starts retried: attempted=%d later=%d", attempted, later)
	}
}

func TestMigrationBackgroundActivationConcurrentRequestIsRejected(t *testing.T) {
	prepareMigrationBackgroundActivation(t)
	entered, release := make(chan struct{}), make(chan struct{})
	var count atomic.Int64
	startMigrationBackgroundWorker("blocking", func() { count.Add(1); close(entered); <-release })
	result := make(chan error, 1)
	go func() { result <- ActivateMigrationBackgroundWorkers() }()
	<-entered
	if err := ActivateMigrationBackgroundWorkers(); err == nil {
		t.Fatal("concurrent activation accepted")
	}
	if _, err := BeginMigrationWriterQuiesce(context.Background()); err == nil {
		t.Fatal("quiet acquired during activation")
	}
	close(release)
	if err := <-result; err != nil {
		t.Fatal(err)
	}
	if count.Load() != 1 {
		t.Fatal("callback started twice")
	}
}
