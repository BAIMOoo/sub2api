package repository

import (
	"context"
	"fmt"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/lib/pq"
)

const opsOutputTPSExpression = "ul.output_tokens * 1000.0 / NULLIF(ul.duration_ms, 0)"

func buildOutputTPSWhere(filter *service.OpsDashboardFilter) (join, where string, args []any, nextIndex int) {
	join, where, args, nextIndex = buildUsageWhere(filter, filter.StartTime.UTC(), filter.EndTime.UTC(), 1)
	where += fmt.Sprintf(`
 AND ul.output_tokens > 0 AND ul.duration_ms > 0
 AND ul.image_count = 0 AND ul.image_output_tokens = 0
 AND COALESCE(ul.billing_mode, '') <> 'image'
 AND ul.request_type IN (%d, %d, %d, %d)`,
		service.RequestTypeSync, service.RequestTypeStream, service.RequestTypeWSV2, service.RequestTypeCyberBlocked)
	return join, where, args, nextIndex
}

func (r *opsRepository) GetModelTPSTrend(ctx context.Context, filter *service.OpsDashboardFilter, bucketSeconds int, models []string) (*service.OpsModelTPSTrendResponse, error) {
	if r == nil || r.db == nil {
		return nil, fmt.Errorf("nil ops repository")
	}
	if filter == nil || filter.StartTime.IsZero() || filter.EndTime.IsZero() {
		return nil, fmt.Errorf("start_time/end_time required")
	}
	if bucketSeconds <= 0 {
		return nil, fmt.Errorf("bucket_seconds must be positive")
	}

	result := &service.OpsModelTPSTrendResponse{
		StartTime:     filter.StartTime.UTC(),
		EndTime:       filter.EndTime.UTC(),
		BucketSeconds: bucketSeconds,
		Models:        []string{},
		Points:        []service.OpsModelTPSPoint{},
	}
	join, where, args, nextIndex := buildOutputTPSWhere(filter)
	modelExpression := resolveModelDimensionExpressionWithAlias("requested", "ul")
	if len(models) == 0 {
		query := "SELECT DISTINCT " + modelExpression + " AS model FROM usage_logs ul " + join + " " + where + " ORDER BY model"
		rows, err := r.db.QueryContext(ctx, query, args...)
		if err != nil {
			return nil, err
		}
		defer func() { _ = rows.Close() }()
		for rows.Next() {
			var model string
			if err := rows.Scan(&model); err != nil {
				return nil, err
			}
			result.Models = append(result.Models, model)
		}
		return result, rows.Err()
	}

	query := fmt.Sprintf(`
SELECT model, bucket_start,
       percentile_cont(0.50) WITHIN GROUP (ORDER BY output_tps),
       percentile_cont(0.10) WITHIN GROUP (ORDER BY output_tps),
       COUNT(*)
FROM (
  SELECT %s AS model,
         date_bin(make_interval(secs => $%d), ul.created_at, TIMESTAMPTZ '1970-01-01 00:00:00+00') AS bucket_start,
         %s AS output_tps
  FROM usage_logs ul
  %s
  %s
  AND %s = ANY($%d::text[])
) samples
GROUP BY model, bucket_start
ORDER BY bucket_start, model`,
		modelExpression, nextIndex+1, opsOutputTPSExpression, join, where, modelExpression, nextIndex)
	args = append(args, pq.Array(models), bucketSeconds)
	rows, err := r.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	for rows.Next() {
		var point service.OpsModelTPSPoint
		if err := rows.Scan(&point.Model, &point.BucketStart, &point.P50, &point.P10, &point.SampleCount); err != nil {
			return nil, err
		}
		point.BucketStart = point.BucketStart.UTC()
		result.Points = append(result.Points, point)
	}
	return result, rows.Err()
}
