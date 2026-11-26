package service

import (
	"context"
	"time"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
)

func (s *OpsService) GetModelTPSTrend(ctx context.Context, filter *OpsDashboardFilter, bucketSeconds int, models []string) (*OpsModelTPSTrendResponse, error) {
	if err := s.RequireMonitoringEnabled(ctx); err != nil {
		return nil, err
	}
	if s.opsRepo == nil {
		return nil, infraerrors.ServiceUnavailable("OPS_REPO_UNAVAILABLE", "Ops repository not available")
	}
	if filter == nil || filter.StartTime.IsZero() || filter.EndTime.IsZero() || filter.StartTime.After(filter.EndTime) {
		return nil, infraerrors.BadRequest("OPS_TIME_RANGE_INVALID", "A valid time range is required")
	}

	queryCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	result, err := s.opsRepo.GetModelTPSTrend(queryCtx, filter, bucketSeconds, models)
	if err != nil && queryCtx.Err() == context.DeadlineExceeded {
		return nil, infraerrors.ServiceUnavailable("OPS_MODEL_TPS_TIMEOUT", "Model TPS query timed out")
	}
	return result, err
}
