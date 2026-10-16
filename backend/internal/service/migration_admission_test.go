package service

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func TestMigrationIngressQueuesKeepaliveWhileExistingStreamCompletes(t *testing.T) {
	runtime := NewMigrationRuntime()
	started, finish := make(chan struct{}), make(chan struct{})
	var newHandled atomic.Int64
	server := httptest.NewServer(runtime.WrapHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/stream" {
			w.Header().Set("Content-Type", "text/event-stream")
			_, _ = io.WriteString(w, "data: before\n\n")
			w.(http.Flusher).Flush()
			close(started)
			<-finish
			_, _ = io.WriteString(w, "data: after\n\n")
			return
		}
		if r.URL.Path == "/new" {
			newHandled.Add(1)
		}
		_, _ = io.WriteString(w, "complete")
	})))
	defer server.Close()
	transport := &http.Transport{MaxIdleConnsPerHost: 1}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 3 * time.Second}
	warm, err := client.Get(server.URL + "/warm")
	require.NoError(t, err)
	_, err = io.Copy(io.Discard, warm.Body)
	require.NoError(t, err)
	warm.Body.Close()
	streamTransport := &http.Transport{}
	defer streamTransport.CloseIdleConnections()
	streamClient := &http.Client{Transport: streamTransport, Timeout: 3 * time.Second}
	stream, err := streamClient.Get(server.URL + "/stream")
	require.NoError(t, err)
	defer stream.Body.Close()
	<-started
	paused := runtime.PauseIngress()
	require.False(t, paused.Quiet)
	require.Error(t, runtime.AssertIngressQuiet())
	var reused atomic.Bool
	ctx := httptrace.WithClientTrace(context.Background(), &httptrace.ClientTrace{GotConn: func(info httptrace.GotConnInfo) { reused.Store(info.Reused) }})
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, server.URL+"/new", nil)
	require.NoError(t, err)
	type outcome struct {
		body string
		err  error
	}
	done := make(chan outcome, 1)
	go func() {
		res, err := client.Do(req)
		if err != nil {
			done <- outcome{err: err}
			return
		}
		defer res.Body.Close()
		body, err := io.ReadAll(res.Body)
		done <- outcome{body: string(body), err: err}
	}()
	require.Eventually(t, func() bool { return runtime.Snapshot().Ingress.QueuedRequests == 1 }, time.Second, time.Millisecond)
	require.True(t, reused.Load(), "the gate must also cover an existing keepalive connection")
	require.Zero(t, newHandled.Load())
	close(finish)
	streamBody, err := io.ReadAll(stream.Body)
	require.NoError(t, err)
	require.Equal(t, "data: before\n\ndata: after\n\n", string(streamBody))
	require.NoError(t, runtime.WaitForRequests(context.Background()))
	require.NoError(t, runtime.AssertIngressQuiet())
	require.Error(t, runtime.ResumeIngress(paused.Epoch+99))
	require.NoError(t, runtime.AssertIngressQuiet(), "a stale resume cannot release the gate")
	require.NoError(t, runtime.ResumeIngress(paused.Epoch))
	result := <-done
	require.NoError(t, result.err)
	require.Equal(t, "complete", result.body)
	require.EqualValues(t, 1, newHandled.Load())
	require.Zero(t, runtime.Snapshot().Ingress.QueuedRequests)
	require.Error(t, runtime.AssertIngressQuiet())
}

func TestMigrationIngressCancellationNeverEntersHandler(t *testing.T) {
	runtime := NewMigrationRuntime()
	runtime.PauseIngress()
	ctx, cancel := context.WithCancel(context.Background())
	request := httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader("{}")).WithContext(ctx)
	var handled atomic.Bool
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		runtime.WrapHandler(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { handled.Store(true) })).ServeHTTP(httptest.NewRecorder(), request)
	}()
	require.Eventually(t, func() bool { return runtime.Snapshot().Ingress.QueuedRequests == 1 }, time.Second, time.Millisecond)
	cancel()
	<-finished
	require.False(t, handled.Load())
	require.Zero(t, runtime.Snapshot().Ingress.QueuedRequests)
	require.NoError(t, runtime.AssertIngressQuiet())
}

func TestMigrationIngressControlRequiresTokenAndExactEpoch(t *testing.T) {
	runtime := NewMigrationRuntime()
	handler := runtime.ControlHandler("secret")
	call := func(path, token, body string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
		r.Header.Set("Authorization", "Bearer "+token)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	require.Equal(t, http.StatusUnauthorized, call("/data/ingress/pause", "wrong", "").Code)
	require.False(t, runtime.Snapshot().Ingress.Paused)
	require.Equal(t, http.StatusOK, call("/data/ingress/pause", "secret", "").Code)
	require.Equal(t, http.StatusBadRequest, call("/data/ingress/resume", "secret", `{}`).Code)
	require.Equal(t, http.StatusConflict, call("/data/ingress/resume", "secret", `{"expected_epoch":0}`).Code)
	require.Equal(t, http.StatusBadRequest, call("/data/ingress/resume", "secret", `{"expected_epoch":1} {}`).Code)
	require.NoError(t, runtime.AssertIngressQuiet())
	require.Equal(t, http.StatusNoContent, call("/data/ingress/resume", "secret", `{"expected_epoch":1}`).Code)
}
