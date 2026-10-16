package main

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

func TestMigrationControlRejectsCredentialsAndTrailingRequests(t *testing.T) {
	for _, body := range []string{
		`{}`, `{"expected_epoch":0,"dsn":"secret"}`, `{"expected_epoch":0} {}`, `{"expected_epoch":0,"password":"secret"}`,
	} {
		_, _, err := migrationControlRequest(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/", strings.NewReader(body)))
		require.Error(t, err, body)
	}
	epoch, endpoint, err := migrationControlRequest(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, "/", strings.NewReader(`{"expected_epoch":2,"endpoint":"target"}`)))
	require.NoError(t, err)
	require.EqualValues(t, 2, epoch)
	require.Equal(t, "target", endpoint)
}

func TestMigrationBackgroundControlRejectsUnreadyAuthorityWithoutStartingWorkers(t *testing.T) {
	c := &migrationDataControls{}
	before := service.MigrationBackgroundActivationStatus()
	for _, initialized := range []bool{false, true} {
		c.initialized.Store(initialized)
		for _, held := range []bool{false, true} {
			c.held.Store(held)
			for _, body := range []string{`{}`, `{"expected_epoch":0}`, `{"expected_epoch":1}`, `{"expected_epoch":0,"endpoint":"target"}`} {
				w := httptest.NewRecorder()
				c.backgroundControl(w, httptest.NewRequest(http.MethodPost, "/data/background/activate", strings.NewReader(body)))
				require.GreaterOrEqual(t, w.Code, 400)
				require.Zero(t, c.backgroundEpoch.Load())
			}
		}
	}
	require.Equal(t, before, service.MigrationBackgroundActivationStatus())
}

func TestMigrationWriterControlCannotAcquireBeforeInitialize(t *testing.T) {
	c := &migrationDataControls{}
	r := httptest.NewRequest(http.MethodPost, "/data/writers/pause", strings.NewReader(`{"expected_epoch":0}`))
	w := httptest.NewRecorder()
	c.writerControl(w, r)
	require.Equal(t, http.StatusConflict, w.Code)
	require.False(t, c.held.Load())
	require.Zero(t, c.writerEpoch.Load())
}
