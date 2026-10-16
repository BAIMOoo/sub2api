package securityaudit

import (
	"context"
	"errors"
	"sync"

	"github.com/Wei-Shaw/sub2api/internal/service"
)

// BeginMigrationQuiet does not cancel accepted scans or remove durable jobs.
// It fences enqueue and worker/reclaimer producers and waits for active work.
// Pending database jobs remain authoritative in the replicated database.
func (s *PromptService) BeginMigrationQuiet(ctx context.Context) (func(), error) {
	if s == nil || s.runner == nil {
		return nil, errors.New("migration prompt audit runner is unavailable")
	}
	var resumes []func()
	var once sync.Once
	resume := func() {
		once.Do(func() {
			for i := len(resumes) - 1; i >= 0; i-- {
				resumes[i]()
			}
		})
	}
	for _, gate := range []*service.MigrationWorkGate{&s.migrationEnqueueGate, &s.runner.migrationWorkGate} {
		release, err := gate.Pause(ctx)
		if release != nil {
			resumes = append(resumes, release)
		}
		if err != nil {
			return resume, err
		}
	}
	return resume, nil
}

func (s *PromptService) MigrationTaskStats() service.MigrationTaskStats {
	if s == nil {
		return service.MigrationTaskStats{}
	}
	stats := s.migrationEnqueueGate.Stats()
	if s.runner != nil {
		stats.Active += s.runner.migrationWorkGate.Stats().Active
	}
	return stats
}
