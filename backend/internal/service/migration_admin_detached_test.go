package service

import (
	"context"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
)

type migrationCleanupBlockedRepo struct {
	*cleanupRepoStub
	entered, release chan struct{}
}

func (r *migrationCleanupBlockedRepo) ClaimNextPendingTask(context.Context, int64) (*UsageCleanupTask, error) {
	close(r.entered)
	<-r.release
	return nil, nil
}

type migrationDashboardBlockedRepo struct {
	*dashboardAggregationRepoTestStub
	entered, release chan struct{}
}

func (r *migrationDashboardBlockedRepo) AggregateRange(context.Context, time.Time, time.Time) error {
	close(r.entered)
	<-r.release
	return nil
}

func (r *migrationDashboardBlockedRepo) RecomputeRange(ctx context.Context, a, b time.Time) error {
	return r.AggregateRange(ctx, a, b)
}

func TestMigrationDetachedAdminWorkOutlivesReturnedRequest(t *testing.T) {
	for _, operation := range []string{"cleanup", "backfill", "recompute"} {
		t.Run(operation, func(t *testing.T) {
			enableMigrationDetachedTest(t)
			entered, release := make(chan struct{}), make(chan struct{})
			start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
			end := start.Add(time.Hour)
			if operation == "cleanup" {
				repo := &migrationCleanupBlockedRepo{&cleanupRepoStub{}, entered, release}
				svc := NewUsageCleanupService(repo, nil, nil, &config.Config{UsageCleanup: config.UsageCleanupConfig{Enabled: true}})
				if _, err := svc.CreateTask(context.Background(), UsageCleanupFilters{StartTime: start, EndTime: end}, 1); err != nil {
					t.Fatal(err)
				}
			} else {
				repo := &migrationDashboardBlockedRepo{&dashboardAggregationRepoTestStub{}, entered, release}
				svc := NewDashboardAggregationService(repo, nil, &config.Config{DashboardAgg: config.DashboardAggregationConfig{Enabled: true, BackfillEnabled: true}})
				var err error
				if operation == "backfill" {
					err = svc.TriggerBackfill(start, end)
				} else {
					err = svc.TriggerRecomputeRange(start, end)
				}
				if err != nil {
					t.Fatal(err)
				}
			}
			<-entered
			requireMigrationDetachedActive(t, 1)
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			if WaitForMigrationDetachedTasks(ctx) == nil {
				t.Fatal("returned HTTP work falsely counted as quiet")
			}
			close(release)
			ctx, cancel = context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			if err := WaitForMigrationDetachedTasks(ctx); err != nil {
				t.Fatal(err)
			}
		})
	}
}
