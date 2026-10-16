package service

import (
	"context"
	"errors"
)

func (s *APIKeyService) MigrationAuthSubscriptionRequired() bool {
	return s != nil && s.cache != nil && (s.authCacheL1 != nil || s.authNegativeCacheL1 != nil)
}

func (s *SubscriptionService) MigrationSubscriptionSubscriberRequired() bool {
	return s != nil && s.billingCacheService != nil && s.subCacheL1 != nil
}

// These helpers require closed global admission and zero in-flight HTTP,
// WebSocket and background cache readers/writers. Clearing a cache while a
// previously admitted reader can repopulate it does not reconstruct an epoch.
func (s *APIKeyService) ClearMigrationAuthCache(_ context.Context) error {
	if s == nil {
		return nil
	}
	if s.authCacheL1 != nil {
		s.authCacheL1.Clear()
		s.authCacheL1.Wait()
	}
	if s.authNegativeCacheL1 != nil {
		s.authNegativeCacheL1.Clear()
		s.authNegativeCacheL1.Wait()
	}
	return nil
}

func (s *SubscriptionService) ClearMigrationSubscriptionCache(_ context.Context) error {
	if s != nil && s.subCacheL1 != nil {
		s.subCacheL1.Clear()
		s.subCacheL1.Wait()
	}
	return nil
}

func (s *ChannelService) ClearMigrationChannelCache(_ context.Context) error {
	if s != nil {
		s.clearCache()
	}
	return nil
}

func clearMigrationRepositoryLocalCache(ctx context.Context, cache any) error {
	if cache == nil {
		return nil
	}
	clearer, ok := cache.(interface{ ClearMigrationLocalCache(context.Context) error })
	if !ok {
		return errors.New("migration local cache clearer is not connected")
	}
	return clearer.ClearMigrationLocalCache(ctx)
}

func (s *ErrorPassthroughService) RebuildMigrationErrorCache(ctx context.Context) error {
	if s == nil {
		return nil
	}
	s.clearLocalCache()
	if err := clearMigrationRepositoryLocalCache(ctx, s.cache); err != nil {
		return err
	}
	rules, err := s.repo.List(ctx)
	if err != nil {
		return err
	}
	if s.cache != nil {
		if err := s.cache.Set(ctx, rules); err != nil {
			return err
		}
	}
	s.setLocalCache(rules)
	return nil
}

func (s *TLSFingerprintProfileService) RebuildMigrationTLSCache(ctx context.Context) error {
	if s == nil {
		return nil
	}
	s.localMu.Lock()
	s.localCache = nil
	s.localMu.Unlock()
	s.randomMu.Lock()
	s.randomByAccount = nil
	s.randomMu.Unlock()
	if err := clearMigrationRepositoryLocalCache(ctx, s.cache); err != nil {
		return err
	}
	profiles, err := s.repo.List(ctx)
	if err != nil {
		return err
	}
	if s.cache != nil {
		if err := s.cache.Set(ctx, profiles); err != nil {
			return err
		}
	}
	s.setLocalCache(profiles)
	return nil
}
