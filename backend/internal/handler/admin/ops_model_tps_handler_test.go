package admin

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

type modelTPSTrendTestRepo struct {
	service.OpsRepository
	query func(*service.OpsDashboardFilter, int, []string) (*service.OpsModelTPSTrendResponse, error)
}

func (r modelTPSTrendTestRepo) GetModelTPSTrend(_ context.Context, filter *service.OpsDashboardFilter, bucketSeconds int, models []string) (*service.OpsModelTPSTrendResponse, error) {
	return r.query(filter, bucketSeconds, models)
}

func TestOpsModelTPSTrendHandler(t *testing.T) {
	gin.SetMode(gin.TestMode)
	start := time.Date(2026, 11, 26, 0, 0, 0, 0, time.UTC)
	end := start.Add(6 * time.Hour)
	repo := modelTPSTrendTestRepo{
		query: func(filter *service.OpsDashboardFilter, bucketSeconds int, models []string) (*service.OpsModelTPSTrendResponse, error) {
			require.True(t, filter.StartTime.Equal(start))
			require.True(t, filter.EndTime.Equal(end))
			require.Equal(t, "openai", filter.Platform)
			require.NotNil(t, filter.GroupID)
			require.EqualValues(t, 17, *filter.GroupID)
			require.Equal(t, 300, bucketSeconds)
			require.Equal(t, []string{"model/a", "model-b"}, models)
			return &service.OpsModelTPSTrendResponse{
				StartTime: start, EndTime: end, BucketSeconds: bucketSeconds,
				Models: []string{},
				Points: []service.OpsModelTPSPoint{
					{Model: "model/a", BucketStart: start, P50: 31, P10: 22, SampleCount: 4},
				},
			}, nil
		},
	}
	handler := NewOpsHandler(service.NewOpsService(repo, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil))
	router := gin.New()
	const path = "/api/v1/admin/ops/dashboard/model-tps-trend"
	router.GET(path, handler.GetDashboardModelTPSTrend)

	writer := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, path+"?start_time=2026-11-26T00:00:00Z&end_time=2026-11-26T06:00:00Z&platform=openai&group_id=17&model=model%2Fa&model=model-b", nil)
	router.ServeHTTP(writer, req)
	require.Equal(t, http.StatusOK, writer.Code)
	require.JSONEq(t, `{
		"code": 0,
		"message": "success",
		"data": {
			"start_time": "2026-11-26T00:00:00Z",
			"end_time": "2026-11-26T06:00:00Z",
			"bucket_seconds": 300,
			"models": [],
			"points": [{"model":"model/a","bucket_start":"2026-11-26T00:00:00Z","p50":31,"p10":22,"sample_count":4}]
		}
	}`, writer.Body.String())

	writer = httptest.NewRecorder()
	router.ServeHTTP(writer, httptest.NewRequest(http.MethodGet, path+"?group_id=0", nil))
	require.Equal(t, http.StatusBadRequest, writer.Code)
}
