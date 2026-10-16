package service

import (
	"context"
	"fmt"
	"sync"

	"golang.org/x/sync/singleflight"
)

var migrationDetachedWork migrationWorkCounter
var migrationDetachedSites = struct {
	sync.Mutex
	active map[string]uint64
}{active: make(map[string]uint64)}

// BeginMigrationDetachedTask must run in the submitting goroutine before
// launching/queueing work. Registering inside the new goroutine leaves a gap
// in which ingress can be quiet while accepted work is still invisible.
// The returned completion is idempotent and must run after all data access.
func BeginMigrationDetachedTask(name string) func() {
	if !MigrationModeEnabled() {
		return func() {}
	}
	ticket := migrationDetachedWork.accept()
	ticket.start()
	migrationDetachedSites.Lock()
	migrationDetachedSites.active[name]++
	migrationDetachedSites.Unlock()
	var once sync.Once
	return func() {
		once.Do(func() {
			migrationDetachedSites.Lock()
			migrationDetachedSites.active[name]--
			if migrationDetachedSites.active[name] == 0 {
				delete(migrationDetachedSites.active, name)
			}
			migrationDetachedSites.Unlock()
			ticket.finish(false)
		})
	}
}

func MigrationDetachedTaskSites() map[string]uint64 {
	migrationDetachedSites.Lock()
	defer migrationDetachedSites.Unlock()
	sites := make(map[string]uint64, len(migrationDetachedSites.active))
	for name, count := range migrationDetachedSites.active {
		sites[name] = count
	}
	return sites
}

// DoChan launches work internally, and even a canceled/follower caller can
// return before that work finishes. Track the shared result independently of
// request lifetime; the forwarded channel retains singleflight's buffer size.
func MigrationSingleflightDoChan(name string, group *singleflight.Group, key string, work func() (any, error)) <-chan singleflight.Result {
	if !MigrationModeEnabled() {
		return group.DoChan(key, work)
	}
	done := BeginMigrationDetachedTask(name)
	result := group.DoChan(key, work)
	forward := make(chan singleflight.Result, 1)
	go func() { defer done(); forward <- <-result }()
	return forward
}

func MigrationDetachedTaskStats() MigrationTaskStats { return migrationDetachedWork.snapshot() }

func WaitForMigrationDetachedTasks(ctx context.Context) error {
	if err := migrationDetachedWork.wait(ctx); err != nil {
		return fmt.Errorf("migration detached tasks remain %v: %w", MigrationDetachedTaskSites(), err)
	}
	return nil
}
