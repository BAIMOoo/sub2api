package service

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/model"
	"github.com/dgraph-io/ristretto"
	"github.com/stretchr/testify/require"
)

func migrationTestL1(t *testing.T) *ristretto.Cache {
	t.Helper()
	cache, err := ristretto.NewCache(&ristretto.Config{NumCounters: 100, MaxCost: 10, BufferItems: 64, IgnoreInternalCost: true})
	require.NoError(t, err)
	t.Cleanup(cache.Close)
	return cache
}

func TestMigrationClearsPositiveNegativeAndSubscriptionL1(t *testing.T) {
	ctx := context.Background()
	s := &APIKeyService{authCacheL1: migrationTestL1(t), authNegativeCacheL1: migrationTestL1(t),
		authCfg: apiKeyAuthCacheConfig{l1TTL: time.Minute, negativeTTL: time.Minute}}
	s.setAuthCacheL1("positive", &APIKeyAuthCacheEntry{Snapshot: &APIKeyAuthSnapshot{Version: apiKeyAuthSnapshotVersion}})
	s.setAuthCacheL1("negative", &APIKeyAuthCacheEntry{NotFound: true})
	s.authCacheL1.Wait()
	s.authNegativeCacheL1.Wait()
	_, found := s.getAuthCacheEntry(ctx, "positive")
	require.True(t, found)
	_, found = s.getAuthCacheEntry(ctx, "negative")
	require.True(t, found)
	require.NoError(t, s.ClearMigrationAuthCache(ctx))
	_, found = s.getAuthCacheEntry(ctx, "positive")
	require.False(t, found)
	_, found = s.getAuthCacheEntry(ctx, "negative")
	require.False(t, found)
	sub := &SubscriptionService{subCacheL1: migrationTestL1(t)}
	require.True(t, sub.subCacheL1.Set("old", "subscription", 1))
	sub.subCacheL1.Wait()
	require.NoError(t, sub.ClearMigrationSubscriptionCache(ctx))
	_, found = sub.subCacheL1.Get("old")
	require.False(t, found)
}

type migrationErrorRepo struct {
	ErrorPassthroughRepository
	rules []*model.ErrorPassthroughRule
	err   error
}

func (r migrationErrorRepo) List(context.Context) ([]*model.ErrorPassthroughRule, error) {
	return r.rules, r.err
}

type migrationTLSRepo struct {
	TLSFingerprintProfileRepository
	profiles []*model.TLSFingerprintProfile
	err      error
}

func (r migrationTLSRepo) List(context.Context) ([]*model.TLSFingerprintProfile, error) {
	return r.profiles, r.err
}

func TestMigrationRebuildDiscardedOldServiceSnapshotsAndFailClosed(t *testing.T) {
	ctx := context.Background()
	errLoad := errors.New("new primary unavailable")
	errorService := &ErrorPassthroughService{repo: migrationErrorRepo{rules: []*model.ErrorPassthroughRule{{ID: 2}}}}
	errorService.setLocalCache([]*model.ErrorPassthroughRule{{ID: 1}})
	require.NoError(t, errorService.RebuildMigrationErrorCache(ctx))
	require.EqualValues(t, 2, errorService.localCache[0].ID)
	errorService.repo = migrationErrorRepo{err: errLoad}
	require.ErrorIs(t, errorService.RebuildMigrationErrorCache(ctx), errLoad)
	require.Nil(t, errorService.localCache)
	tlsService := &TLSFingerprintProfileService{repo: migrationTLSRepo{profiles: []*model.TLSFingerprintProfile{{ID: 2}}}}
	tlsService.setLocalCache([]*model.TLSFingerprintProfile{{ID: 1}})
	require.NoError(t, tlsService.RebuildMigrationTLSCache(ctx))
	require.NotNil(t, tlsService.localCache[2])
	require.Nil(t, tlsService.localCache[1])
	tlsService.repo = migrationTLSRepo{err: errLoad}
	require.ErrorIs(t, tlsService.RebuildMigrationTLSCache(ctx), errLoad)
	require.Nil(t, tlsService.localCache)
	require.Nil(t, tlsService.randomByAccount)
	channel := &ChannelService{}
	channel.cache.Store(&channelCache{})
	require.NoError(t, channel.ClearMigrationChannelCache(ctx))
	require.Nil(t, channel.cache.Load().(*channelCache))
}
