package repository

import (
	"context"
	"errors"
	"os"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
	"github.com/stretchr/testify/require"
)

func exerciseMigrationRedisRebuild(t *testing.T, sourceAddress, targetAddress string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	options := migrationRedisTestOptions(sourceAddress)
	options.MinIdleConns = 4 // exercise asynchronous pool dials during rebuild
	client, h := newMigrationRedisTestClient(t, options)
	require.Same(t, h, MigrationRedisHandoverForClient(client))
	var quiet atomic.Bool
	var l1 atomic.Int64
	l1.Store(100)
	messages := make(chan string, 8)
	done, err := h.SubscribeMigration(ctx, service.MigrationRedisSubscription{
		Name: "actual-consumer", Channel: "migration-rebuild-test",
		Handle: func(_ context.Context, payload string) { messages <- payload },
	})
	require.NoError(t, err)
	_, err = h.SubscribeMigration(ctx, service.MigrationRedisSubscription{
		Name: "actual-consumer", Channel: "migration-rebuild-test", Handle: func(context.Context, string) {},
	})
	require.ErrorContains(t, err, "already registered")
	require.Error(t, h.ConfigureRebuild(MigrationRedisRebuildPlan{
		RequiredSubscriptions: []string{"omitted-consumer"},
		CacheRebuilders:       map[string]func(context.Context) error{"l1": func(context.Context) error { return nil }},
		AssertQuiescent:       func() error { return nil },
	}))
	require.NoError(t, h.ConfigureRebuild(MigrationRedisRebuildPlan{
		RequiredSubscriptions: []string{"actual-consumer"},
		CacheRebuilders: map[string]func(context.Context) error{"l1": func(rebuildCtx context.Context) error {
			// Rebuild commands are explicitly admitted while ordinary commands
			// remain blocked. Inspecting a target value proves the new endpoint.
			value, err := client.Get(rebuildCtx, "migration-rebuild-target").Int64()
			if err != nil {
				return err
			}
			l1.Store(value)
			return nil
		}},
		AssertQuiescent: func() error {
			if !quiet.Load() {
				return errors.New("HTTP/L1 readers still admitted")
			}
			return nil
		},
	}))
	source := redis.NewClient(migrationRedisTestOptions(sourceAddress))
	target := redis.NewClient(migrationRedisTestOptions(targetAddress))
	t.Cleanup(func() { _ = source.Close(); _ = target.Close() })
	require.NoError(t, target.Set(ctx, "migration-rebuild-target", 200, 0).Err())
	require.NoError(t, source.Publish(ctx, "migration-rebuild-test", "old-generation").Err())
	select {
	case value := <-messages:
		require.Equal(t, "old-generation", value)
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	require.NoError(t, h.Pause(ctx))
	// Regression: Redis's command barrier does not prevent a stale L1 hit.
	// Reconstruction must require the separate global quiescence proof.
	require.EqualValues(t, 100, l1.Load())
	_, err = h.SwitchPaused(targetAddress, 1)
	require.NoError(t, err)
	require.ErrorContains(t, h.RebuildPaused(ctx, 2), "global quiescence")
	require.Error(t, h.Resume())
	require.True(t, h.Status().Paused)
	require.False(t, h.Status().PubSubCacheReady)
	quiet.Store(true)
	require.NoError(t, h.RebuildPaused(ctx, 2))
	require.NoError(t, h.Readiness())
	require.EqualValues(t, 200, l1.Load())
	require.EqualValues(t, 2, h.ConsumerStatus()[0].Generation)
	// PUBSUB NUMSUB on the target is independent confirmation that ACKs
	// did not come from a surviving source connection.
	counts, err := target.PubSubNumSub(ctx, "migration-rebuild-test").Result()
	require.NoError(t, err)
	require.EqualValues(t, 1, counts["migration-rebuild-test"])
	counts, err = source.PubSubNumSub(ctx, "migration-rebuild-test").Result()
	require.NoError(t, err)
	require.Zero(t, counts["migration-rebuild-test"])
	require.NoError(t, h.Resume())
	require.NoError(t, target.Publish(ctx, "migration-rebuild-test", "new-generation").Err())
	select {
	case value := <-messages:
		require.Equal(t, "new-generation", value)
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	cancel()
	select {
	case err := <-done:
		require.ErrorIs(t, err, context.Canceled)
	case <-time.After(time.Second):
		t.Fatal("managed consumer did not stop")
	}
}

func TestMigrationRedisRebuildRequiresGlobalQuietAndNewEndpointACK(t *testing.T) {
	source, target := miniredis.RunT(t), miniredis.RunT(t)
	exerciseMigrationRedisRebuild(t, source.Addr(), target.Addr())
}

func TestMigrationRedisRebuildRealRedis(t *testing.T) {
	source, target := os.Getenv("MIGRATION_REDIS_TEST_SOURCE_ADDR"), os.Getenv("MIGRATION_REDIS_TEST_TARGET_ADDR")
	if source == "" || target == "" {
		t.Skip("set two isolated real Redis addresses to exercise server ACKs")
	}
	if source == target {
		t.Fatal("real Redis source and target must be separate isolated instances")
	}
	exerciseMigrationRedisRebuild(t, source, target)
}

func TestMigrationRedisPauseWaitsForOldPubSubHandlerAndFailsClosed(t *testing.T) {
	server := miniredis.RunT(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	client, h := newMigrationRedisTestClient(t, migrationRedisTestOptions(server.Addr()))
	entered, release := make(chan struct{}), make(chan struct{})
	_, err := h.SubscribeMigration(ctx, service.MigrationRedisSubscription{
		Name: "slow-handler", Channel: "slow-handler", Handle: func(context.Context, string) { close(entered); <-release },
	})
	require.NoError(t, err)
	require.NoError(t, client.Publish(ctx, "slow-handler", "value").Err())
	<-entered
	deadline, stop := context.WithTimeout(ctx, 20*time.Millisecond)
	defer stop()
	require.ErrorIs(t, h.Pause(deadline), context.DeadlineExceeded)
	require.True(t, h.Status().Paused)
	require.False(t, h.Status().PubSubCacheReady)
	close(release)
	require.NoError(t, h.Pause(ctx))
}

func TestMigrationRedisInitializationIsExplicitAndFailsOnInvalidConfig(t *testing.T) {
	cfg := &config.Config{Redis: config.RedisConfig{Host: "127.0.0.1", Port: 6379}}
	t.Setenv("MIGRATION_REDIS_ENABLED", "")
	legacy := InitRedis(cfg)
	require.Nil(t, MigrationRedisHandoverForClient(legacy))
	require.NoError(t, legacy.Close())
	t.Setenv("MIGRATION_REDIS_ENABLED", "definitely")
	require.PanicsWithValue(t, "invalid MIGRATION_REDIS_ENABLED: expected a boolean", func() { InitRedis(cfg) })
	t.Setenv("MIGRATION_REDIS_ENABLED", "true")
	client := InitRedis(cfg)
	h := MigrationRedisHandoverForClient(client)
	require.NotNil(t, h)
	require.ErrorIs(t, h.Readiness(), ErrMigrationRedisRebuildUnwired)
	require.NoError(t, h.Close())
	require.Nil(t, MigrationRedisHandoverForClient(client))
	cfg.Redis.EnableTLS = true
	require.Panics(t, func() { InitRedis(cfg) })
}

func TestMigrationRedisAllRepositorySubscribersRebindToTarget(t *testing.T) {
	source, target := miniredis.RunT(t), miniredis.RunT(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	client, h := newMigrationRedisTestClient(t, migrationRedisTestOptions(source.Addr()))
	seen := make(chan string, 10)
	authDone := make(chan error, 1)
	go func() {
		authDone <- NewAPIKeyCache(client).SubscribeAuthCacheInvalidation(ctx, func(string) { seen <- "auth" })
	}()
	require.NoError(t, NewBillingCache(client).(*billingCache).SubscribeSubscriptionCacheInvalidation(ctx, func(string) { seen <- "subscription" }))
	NewChannelCache(client).SubscribeUpdates(ctx, func() { seen <- "channel" })
	errorCache := NewErrorPassthroughCache(client).(*errorPassthroughCache)
	errorCache.SubscribeUpdates(ctx, func() { seen <- "error_passthrough" })
	tlsCache := NewTLSFingerprintProfileCache(client).(*tlsFingerprintProfileCache)
	tlsCache.SubscribeUpdates(ctx, func() { seen <- "tls_fingerprint" })
	require.Eventually(t, func() bool { return len(h.ConsumerStatus()) == 5 }, time.Second, time.Millisecond)
	require.NoError(t, h.ConfigureRebuild(MigrationRedisRebuildPlan{
		RequiredSubscriptions: []string{"auth", "subscription", "channel", "error_passthrough", "tls_fingerprint"},
		CacheRebuilders:       map[string]func(context.Context) error{"error_l1": errorCache.ClearMigrationLocalCache, "tls_l1": tlsCache.ClearMigrationLocalCache},
		AssertQuiescent:       func() error { return nil },
	}))
	require.NoError(t, h.Pause(ctx))
	epoch, err := h.SwitchPaused(target.Addr(), 1)
	require.NoError(t, err)
	require.NoError(t, h.RebuildPaused(ctx, epoch))
	targetClient := redis.NewClient(migrationRedisTestOptions(target.Addr()))
	defer targetClient.Close()
	channels := []string{authCacheInvalidateChannel, subCacheInvalidateChannel, channelCachePubSubKey, errorPassthroughPubSubKey, tlsFPProfilePubSubKey}
	for _, channel := range channels {
		count, err := targetClient.Publish(ctx, channel, "refresh").Result()
		require.NoError(t, err)
		require.EqualValues(t, 1, count)
	}
	// New callbacks may not race the paused reconstruction window.
	select {
	case value := <-seen:
		t.Fatalf("callback %q ran before Redis resume", value)
	default:
	}
	require.NoError(t, h.Resume())
	received := make(map[string]bool)
	for range channels {
		select {
		case value := <-seen:
			received[value] = true
		case <-time.After(time.Second):
			t.Fatal("actual repository subscriber lost a queued target message")
		}
	}
	require.Len(t, received, 5)
	cancel()
	require.ErrorIs(t, <-authDone, context.Canceled)
}
