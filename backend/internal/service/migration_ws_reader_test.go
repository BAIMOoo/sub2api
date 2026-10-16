package service

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	coderws "github.com/coder/websocket"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestMigrationWSReaderBeforeTurnRemainsVisibleAfterHTTPReturn(t *testing.T) {
	enableMigrationDetachedTest(t)
	resetMigrationWriters(t)
	resetMigrationBackgroundWorkers(t)
	gin.SetMode(gin.TestMode)
	for _, name := range MigrationRequiredWriterNames() {
		probe := func() MigrationTaskStats { return MigrationTaskStats{} }
		begin := func(context.Context) (func(), error) { return func() {}, nil }
		if name == "detached_writers" {
			probe = MigrationDetachedTaskStats
			begin = func(ctx context.Context) (func(), error) { return func() {}, WaitForMigrationDetachedTasks(ctx) }
		}
		RegisterMigrationWriterQuiescer(name, probe, begin)
	}
	entered, release, handlerReturned := make(chan struct{}), make(chan struct{}), make(chan struct{})
	var releaseOnce sync.Once
	releasePolicy := func() { releaseOnce.Do(func() { close(release) }) }
	defer releasePolicy()
	var finalDataAccess atomic.Bool
	policyErr := errors.New("later turn policy completed")
	hooks := &OpenAIWSIngressHooks{BeforeTurn: func(turn int) error {
		if turn == 2 {
			close(entered)
			<-release
			// The real hook reads Redis/SQL using the outer control context.
			// Its final access must remain covered even after HTTP has returned.
			finalDataAccess.Store(true)
		}
		return policyErr
	}}
	upstream := newStagedPassthroughConn()
	upstream.Send(`{"type":"response.completed","response":{"id":"first","model":"gpt-5.1","usage":{"input_tokens":1,"output_tokens":1}}}`)
	svc := newPassthroughLifecycleService(passthroughLifecycleConfig(), upstream)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer close(handlerReturned)
		conn, err := coderws.Accept(w, r, nil)
		if err != nil {
			return
		}
		defer conn.CloseNow()
		_, first, err := conn.Read(context.Background())
		if err != nil {
			return
		}
		ginCtx, _ := gin.CreateTestContext(httptest.NewRecorder())
		ginCtx.Request = r.Clone(context.Background())
		_ = svc.ProxyResponsesWebSocketFromClient(context.Background(), ginCtx, conn, passthroughLifecycleAccount(), "sk-test", first, hooks)
	}))
	defer server.Close()
	client := dialPassthroughLifecycleClient(t, server)
	defer client.CloseNow()
	requirePassthroughUpstreamWrite(t, upstream, time.Second)
	_, err := readPassthroughLifecycleFrame(t, client, time.Second)
	require.NoError(t, err)
	writeCtx, cancel := context.WithTimeout(context.Background(), time.Second)
	err = client.Write(writeCtx, coderws.MessageText, []byte(`{"type":"response.create","model":"gpt-5.1"}`))
	cancel()
	require.NoError(t, err)
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("later-turn BeforeTurn callback did not enter")
	}
	// Keep processing the server's existing normal-close handshake while the
	// server-side policy callback is held. This avoids turning the client's
	// missing close ACK into an unrelated multi-second transport wait.
	closeReadCtx, cancelCloseRead := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancelCloseRead()
	clientClosed := make(chan struct{})
	go func() { defer close(clientClosed); _, _, _ = client.Read(closeReadCtx) }()
	upstream.Fail(io.EOF)
	select {
	case <-handlerReturned:
	case <-time.After(2 * time.Second):
		t.Fatal("HTTP handler did not return with its existing bounded client wait")
	}
	select {
	case <-clientClosed:
	case <-time.After(time.Second):
		t.Fatal("WebSocket transport did not close")
	}
	require.False(t, finalDataAccess.Load())
	require.EqualValues(t, 1, MigrationDetachedTaskSites()["openai_ws_v2.client_reader"])
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	lease, err := BeginMigrationWriterQuiesce(ctx)
	cancel()
	require.Nil(t, lease)
	require.ErrorIs(t, err, context.DeadlineExceeded, "accepted policy callback must block a quiet lease")
	releasePolicy()
	ctx, cancel = context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	require.NoError(t, WaitForMigrationDetachedTasks(ctx))
	require.True(t, finalDataAccess.Load())
	require.Zero(t, MigrationDetachedTaskSites()["openai_ws_v2.client_reader"])
	lease, err = BeginMigrationWriterQuiesce(ctx)
	require.NoError(t, err)
	defer lease.Resume()
	require.NoError(t, MigrationWritersQuiet())
}
