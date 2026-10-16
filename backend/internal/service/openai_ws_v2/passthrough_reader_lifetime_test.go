package openai_ws_v2

import (
	"context"
	"errors"
	"io"
	"sync/atomic"
	"testing"
	"time"

	coderws "github.com/coder/websocket"
	"github.com/stretchr/testify/require"
)

func TestRelay_ClientReaderLifetimeIsRegisteredBeforeLaunch(t *testing.T) {
	t.Parallel()
	registered, permitLaunch, read := make(chan struct{}), make(chan struct{}), make(chan struct{})
	var completions atomic.Int64
	done := make(chan struct{})
	go func() {
		defer close(done)
		_, _ = Relay(context.Background(), newPassthroughTestFrameConn(nil, true), newPassthroughTestFrameConn(nil, true), nil, RelayOptions{
			UpstreamDrainTimeout: time.Millisecond,
			BeginClientReader: func() func() {
				close(registered)
				<-permitLaunch
				return func() { completions.Add(1) }
			},
			ReadClientFrame: func(context.Context, FrameConn) (coderws.MessageType, []byte, error) {
				close(read)
				return coderws.MessageText, nil, io.EOF
			},
		})
	}()
	select {
	case <-registered:
	case <-time.After(time.Second):
		t.Fatal("reader admission did not run")
	}
	select {
	case <-read:
		t.Fatal("child launched before parent-side registration completed")
	default:
	}
	require.Zero(t, completions.Load())
	close(permitLaunch)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("relay did not finish")
	}
	require.Eventually(t, func() bool { return completions.Load() == 1 }, time.Second, time.Millisecond)
}

func TestRelay_ClientReaderLifetimeCoversCallbacksAfterReturn(t *testing.T) {
	t.Parallel()
	entered, release, readerDone := make(chan struct{}), make(chan struct{}), make(chan struct{})
	upstream := newPassthroughTestFrameConn(nil, false)
	var active atomic.Int64
	result := make(chan *RelayExit, 1)
	go func() {
		_, relayExit := Relay(context.Background(), newPassthroughTestFrameConn(nil, false), upstream, nil, RelayOptions{
			FirstMessageSent: true,
			BeginClientReader: func() func() {
				active.Add(1)
				return func() { active.Add(-1); close(readerDone) }
			},
			ReadClientFrame: func(context.Context, FrameConn) (coderws.MessageType, []byte, error) {
				close(entered)
				<-release // Represents an outer-context policy / BeforeTurn callback.
				return coderws.MessageText, nil, errors.New("policy rejected")
			},
		})
		result <- relayExit
	}()
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("callback did not enter")
	}
	_ = upstream.Close()
	select {
	case relayExit := <-result:
		require.Nil(t, relayExit, "preserve preexisting graceful-upstream exit")
	case <-time.After(time.Second):
		t.Fatal("relay changed its bounded wait into a blocking client join")
	}
	require.EqualValues(t, 1, active.Load(), "callback still runs after relay return")
	select {
	case <-readerDone:
		t.Fatal("reader lifetime completed while policy callback remained blocked")
	default:
	}
	close(release)
	select {
	case <-readerDone:
	case <-time.After(time.Second):
		t.Fatal("reader completion hook did not finish naturally")
	}
	require.Zero(t, active.Load())
}

func TestRelay_ClientReaderLifetimeCompletionOnExitPaths(t *testing.T) {
	t.Parallel()
	for _, reason := range []string{"read eof", "policy error", "canceled upstream write"} {
		t.Run(reason, func(t *testing.T) {
			t.Parallel()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			var started, completed atomic.Int64
			_, _ = Relay(ctx, newPassthroughTestFrameConn(nil, false), newPassthroughTestFrameConn(nil, false), nil, RelayOptions{
				FirstMessageSent: true, UpstreamDrainTimeout: time.Millisecond,
				BeginClientReader: func() func() {
					started.Add(1)
					return func() { completed.Add(1) }
				},
				ReadClientFrame: func(context.Context, FrameConn) (coderws.MessageType, []byte, error) {
					switch reason {
					case "read eof":
						return coderws.MessageText, nil, io.EOF
					case "policy error":
						return coderws.MessageText, nil, errors.New("policy rejected")
					default:
						cancel()
						return coderws.MessageText, []byte(`{"type":"response.create"}`), nil
					}
				},
			})
			require.EqualValues(t, 1, started.Load())
			require.Eventually(t, func() bool { return completed.Load() == 1 }, time.Second, time.Millisecond)
		})
	}
}

func TestRelay_ClientReaderLifetimeNotStartedWithoutDownstream(t *testing.T) {
	t.Parallel()
	var starts atomic.Int64
	_, relayExit := Relay(context.Background(), newPassthroughTestFrameConn(nil, false), newPassthroughTestFrameConn(nil, true), nil, RelayOptions{
		FirstMessageSent: true, StartClientAfterFirstDownstream: true,
		BeginClientReader: func() func() { starts.Add(1); return nil },
	})
	require.Nil(t, relayExit)
	require.Zero(t, starts.Load(), "unstarted reader must not leak a lifetime ticket")
}
