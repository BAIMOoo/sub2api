package repository

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"net"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
	"github.com/redis/go-redis/v9/maintnotifications"
	"github.com/stretchr/testify/require"
)

func migrationRedisTestOptions(addr string) *redis.Options {
	return &redis.Options{Addr: addr, Protocol: 3, MaxRetries: -1, DialerRetries: -1,
		PoolSize: 8, DialTimeout: time.Second, ReadTimeout: time.Second, WriteTimeout: time.Second,
		ContextTimeoutEnabled: true, DisableIdentity: true}
}

func newMigrationRedisTestClient(t *testing.T, options *redis.Options) (*redis.Client, *MigrationRedisHandover) {
	t.Helper()
	client, handover, err := NewMigrationRedisClient(options)
	require.NoError(t, err)
	t.Cleanup(func() { require.NoError(t, handover.Close()) })
	return client, handover
}

func TestMigrationRedisHooksHoldLeaseThroughCompleteResult(t *testing.T) {
	for _, pipeline := range []bool{false, true} {
		t.Run(fmt.Sprintf("pipeline=%t", pipeline), func(t *testing.T) {
			h := &MigrationRedisHandover{}
			entered := make(chan context.Context, 1)
			finish := make(chan struct{})
			expected := errors.New("command result lost after operation")
			run := func(ctx context.Context) error {
				entered <- ctx
				<-finish
				return expected
			}
			result := make(chan error, 1)
			go func() {
				if pipeline {
					result <- h.ProcessPipelineHook(func(ctx context.Context, _ []redis.Cmder) error { return run(ctx) })(context.Background(), nil)
				} else {
					result <- h.ProcessHook(func(ctx context.Context, _ redis.Cmder) error { return run(ctx) })(context.Background(), nil)
				}
			}()
			operationCtx := <-entered
			pauseCtx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			paused := make(chan error, 1)
			go func() { paused <- h.Pause(pauseCtx) }()
			require.Eventually(t, func() bool { return h.Status().Paused }, time.Second, time.Millisecond)
			require.Equal(t, 1, h.Status().ActiveOperations)
			select {
			case err := <-paused:
				t.Fatalf("pause returned before complete execution: %v", err)
			default:
			}
			// HELLO/AUTH/initialization hooks share the admitted command's lease,
			// including when pause has already closed admission to other commands.
			require.NoError(t, h.ProcessHook(func(context.Context, redis.Cmder) error { return nil })(operationCtx, nil))
			close(finish)
			require.ErrorIs(t, <-result, expected)
			require.NoError(t, <-paused)
			require.Zero(t, h.Status().ActiveOperations)
		})
	}
}

func TestMigrationRedisPoolEpochSwitchWithRetriesDisabled(t *testing.T) {
	source, target := miniredis.RunT(t), miniredis.RunT(t)
	source.RequireAuth("fixed-password")
	target.RequireAuth("fixed-password")
	options := migrationRedisTestOptions(source.Addr())
	options.Password = "fixed-password"
	options.MinIdleConns = 6
	client, handover := newMigrationRedisTestClient(t, options)
	options.Password = "caller-mutation-must-not-affect-new-connections"
	ctx := context.Background()
	require.NoError(t, client.Set(ctx, "before", "source", 0).Err())
	require.Eventually(t, func() bool { return handover.Status().TrackedSockets >= 6 }, time.Second, time.Millisecond)
	require.NoError(t, handover.Pause(ctx))
	epoch, err := handover.SwitchPaused(target.Addr(), 1)
	require.NoError(t, err)
	require.EqualValues(t, 2, epoch)
	require.Zero(t, handover.Status().TrackedSockets)
	require.NoError(t, handover.Resume())
	for i := range 20 {
		require.NoError(t, client.Set(ctx, fmt.Sprintf("after-%d", i), "target", 0).Err())
	}
	_, err = client.Pipelined(ctx, func(pipe redis.Pipeliner) error {
		pipe.Set(ctx, "pipeline-a", "target", 0)
		pipe.Set(ctx, "pipeline-b", "target", 0)
		return nil
	})
	require.NoError(t, err)
	_, err = client.TxPipelined(ctx, func(pipe redis.Pipeliner) error {
		pipe.Set(ctx, "transaction-a", "target", 0)
		pipe.Set(ctx, "transaction-b", "target", 0)
		return nil
	})
	require.NoError(t, err)
	require.Equal(t, []string{"before"}, source.Keys())
	require.Len(t, target.Keys(), 24)
	require.ErrorIs(t, handover.Readiness(), ErrMigrationRedisRebuildUnwired)
	require.False(t, handover.Status().PubSubCacheReady)
}

func TestMigrationRedisPausePreservesCommandDeadlineAndRequiresDrainedEpoch(t *testing.T) {
	source, target := miniredis.RunT(t), miniredis.RunT(t)
	client, handover := newMigrationRedisTestClient(t, migrationRedisTestOptions(source.Addr()))
	_, err := handover.SwitchPaused(target.Addr(), 1)
	require.ErrorContains(t, err, "paused admission")
	require.NoError(t, handover.Pause(context.Background()))
	_, err = handover.SwitchPaused(target.Addr(), 9)
	require.ErrorContains(t, err, "epoch mismatch")
	require.Equal(t, source.Addr(), handover.Status().Address)
	require.True(t, handover.Status().Paused)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	err = client.Set(ctx, "expired", "must-not-run", 0).Err()
	require.ErrorIs(t, err, context.DeadlineExceeded)
	_, err = client.Pipelined(ctx, func(pipe redis.Pipeliner) error {
		pipe.Set(ctx, "expired-pipeline", "must-not-run", 0)
		return nil
	})
	require.ErrorIs(t, err, context.DeadlineExceeded)
	_, err = handover.SwitchPaused(target.Addr(), 1)
	require.NoError(t, err)
	require.NoError(t, handover.Resume())
	require.NoError(t, client.Ping(context.Background()).Err())
	require.Empty(t, source.Keys())
	require.Empty(t, target.Keys())
}

func TestMigrationRedisPauseTimeoutDoesNotReopenAdmission(t *testing.T) {
	h := &MigrationRedisHandover{}
	release, err := h.barrier.acquire(context.Background())
	require.NoError(t, err)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	require.ErrorIs(t, h.Pause(ctx), context.DeadlineExceeded)
	require.True(t, h.Status().Paused)
	_, err = h.SwitchPaused("127.0.0.1:6379", 0)
	require.ErrorContains(t, err, "zero active")
	release()
	require.NoError(t, h.Pause(context.Background()))
	require.True(t, h.Status().Paused)
}

func TestMigrationRedisBackgroundDialIsIncludedInDrain(t *testing.T) {
	source, target := miniredis.RunT(t), miniredis.RunT(t)
	options := migrationRedisTestOptions(source.Addr())
	options.MinIdleConns = 1
	entered, finish := make(chan struct{}), make(chan struct{})
	var first atomic.Bool
	options.Dialer = func(ctx context.Context, network, addr string) (net.Conn, error) {
		conn, err := (&net.Dialer{}).DialContext(ctx, network, addr)
		if err == nil && first.CompareAndSwap(false, true) {
			close(entered)
			select {
			case <-finish:
			case <-ctx.Done():
				_ = conn.Close()
				return nil, ctx.Err()
			}
		}
		return conn, err
	}
	client, handover := newMigrationRedisTestClient(t, options)
	<-entered
	paused := make(chan error, 1)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	go func() { paused <- handover.Pause(ctx) }()
	require.Eventually(t, func() bool { return handover.Status().Paused }, time.Second, time.Millisecond)
	require.Equal(t, 1, handover.Status().ActiveOperations)
	close(finish)
	require.NoError(t, <-paused)
	require.Equal(t, 1, handover.Status().TrackedSockets)
	_, err := handover.SwitchPaused(target.Addr(), 1)
	require.NoError(t, err)
	require.Zero(t, handover.Status().TrackedSockets)
	require.NoError(t, handover.Resume())
	require.NoError(t, client.Set(context.Background(), "after-dial", "target", 0).Err())
	require.False(t, source.Exists("after-dial"))
	require.True(t, target.Exists("after-dial"))
}

func TestMigrationRedisDialRechecksEpochAfterConnect(t *testing.T) {
	source := miniredis.RunT(t)
	entered, finish := make(chan struct{}), make(chan struct{})
	h := &MigrationRedisHandover{epoch: 1, addr: source.Addr(), conns: make(map[*migrationRedisConn]struct{})}
	var actual net.Conn
	h.dial = func(ctx context.Context, network, addr string) (net.Conn, error) {
		conn, err := (&net.Dialer{}).DialContext(ctx, network, addr)
		actual = conn
		close(entered)
		<-finish
		return conn, err
	}
	result := make(chan error, 1)
	go func() { _, err := h.dialEpoch(context.Background(), "tcp", "ignored"); result <- err }()
	<-entered
	// Defensive post-connect fence, independent of the public drain protocol.
	h.mu.Lock()
	h.epoch++
	h.mu.Unlock()
	close(finish)
	require.ErrorContains(t, <-result, "generation changed while dialing")
	require.Zero(t, h.Status().TrackedSockets)
	_, err := actual.Write([]byte("ping"))
	require.Error(t, err)
}

func TestMigrationRedisPubSubSocketReconnectsAndResubscribes(t *testing.T) {
	source, target := miniredis.RunT(t), miniredis.RunT(t)
	client, handover := newMigrationRedisTestClient(t, migrationRedisTestOptions(source.Addr()))
	ctx := context.Background()
	sub := client.Subscribe(ctx, "invalidate")
	t.Cleanup(func() { _ = sub.Close() })
	_, err := sub.Receive(ctx)
	require.NoError(t, err)
	messages := sub.Channel(redis.WithChannelHealthCheckInterval(25 * time.Millisecond))
	sourcePublisher := redis.NewClient(migrationRedisTestOptions(source.Addr()))
	targetPublisher := redis.NewClient(migrationRedisTestOptions(target.Addr()))
	t.Cleanup(func() { _ = sourcePublisher.Close(); _ = targetPublisher.Close() })
	require.NoError(t, sourcePublisher.Publish(ctx, "invalidate", "before").Err())
	select {
	case msg := <-messages:
		require.Equal(t, "before", msg.Payload)
	case <-time.After(time.Second):
		t.Fatal("initial source subscription did not receive")
	}
	require.NoError(t, handover.Pause(ctx))
	_, err = handover.SwitchPaused(target.Addr(), 1)
	require.NoError(t, err)
	require.NoError(t, handover.Resume())
	require.Eventually(t, func() bool {
		counts, err := targetPublisher.PubSubNumSub(ctx, "invalidate").Result()
		return err == nil && counts["invalidate"] == 1
	}, 3*time.Second, 10*time.Millisecond)
	require.Eventually(t, func() bool {
		counts, err := sourcePublisher.PubSubNumSub(ctx, "invalidate").Result()
		return err == nil && counts["invalidate"] == 0
	}, time.Second, 10*time.Millisecond)
	require.NoError(t, targetPublisher.Publish(ctx, "invalidate", "after").Err())
	select {
	case msg := <-messages:
		require.Equal(t, "after", msg.Payload)
	case <-time.After(time.Second):
		t.Fatal("existing PubSub object did not resubscribe on the target")
	}
	// Delivery after resubscribe cannot prove recovery of missed invalidations.
	require.ErrorIs(t, handover.Readiness(), ErrMigrationRedisRebuildUnwired)
}

func TestMigrationRedisRejectsUnverifiedOptionsAndOpaqueTransports(t *testing.T) {
	for name, alter := range map[string]func(*redis.Options){
		"tls":          func(o *redis.Options) { o.TLSConfig = &tls.Config{} },
		"unix":         func(o *redis.Options) { o.Network = "unix" },
		"credentials":  func(o *redis.Options) { o.CredentialsProvider = func() (string, string) { return "", "" } },
		"client_cache": func(o *redis.Options) { o.ClientSideCacheConfig = &redis.ClientSideCacheConfig{} },
		"maintenance": func(o *redis.Options) {
			o.MaintNotificationsConfig = &maintnotifications.Config{Mode: maintnotifications.ModeAuto}
		},
		"url": func(o *redis.Options) { o.Addr = "redis://user:secret@localhost:6379" },
	} {
		t.Run(name, func(t *testing.T) {
			o := migrationRedisTestOptions("127.0.0.1:6379")
			alter(o)
			_, _, err := NewMigrationRedisClient(o)
			require.Error(t, err)
			require.NotContains(t, err.Error(), "secret")
		})
	}
	left, right := net.Pipe()
	defer right.Close()
	h := &MigrationRedisHandover{epoch: 1, addr: "127.0.0.1:6379", conns: make(map[*migrationRedisConn]struct{}),
		dial: func(context.Context, string, string) (net.Conn, error) { return left, nil }}
	_, err := h.dialEpoch(context.Background(), "tcp", "ignored")
	require.ErrorContains(t, err, "opaque transport")
	_, err = right.Write([]byte("closed"))
	require.Error(t, err)
}

func TestMigrationRedisConcurrentCommandsStayOnOneSideOfSwitch(t *testing.T) {
	source, target := miniredis.RunT(t), miniredis.RunT(t)
	client, handover := newMigrationRedisTestClient(t, migrationRedisTestOptions(source.Addr()))
	ctx := context.Background()
	var wg sync.WaitGroup
	errorsSeen := make(chan error, 80)
	for n := range 40 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			errorsSeen <- client.Set(ctx, fmt.Sprintf("before-%d", n), "source", 0).Err()
		}()
	}
	wg.Wait()
	require.NoError(t, handover.Pause(ctx))
	for n := range 40 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			errorsSeen <- client.Set(ctx, fmt.Sprintf("after-%d", n), "target", 0).Err()
		}()
	}
	_, err := handover.SwitchPaused(target.Addr(), 1)
	require.NoError(t, err)
	require.NoError(t, handover.Resume())
	wg.Wait()
	close(errorsSeen)
	for err := range errorsSeen {
		require.NoError(t, err)
	}
	require.Len(t, source.Keys(), 40)
	require.Len(t, target.Keys(), 40)
	for _, key := range target.Keys() {
		require.Contains(t, key, "after-")
	}
}
