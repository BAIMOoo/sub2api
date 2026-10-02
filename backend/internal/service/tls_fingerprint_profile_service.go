package service

import (
	"context"
	"math/rand/v2"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/model"
	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
)

// TLSFingerprintProfileRepository 定义 TLS 指纹模板的数据访问接口
type TLSFingerprintProfileRepository interface {
	List(ctx context.Context) ([]*model.TLSFingerprintProfile, error)
	GetByID(ctx context.Context, id int64) (*model.TLSFingerprintProfile, error)
	Create(ctx context.Context, profile *model.TLSFingerprintProfile) (*model.TLSFingerprintProfile, error)
	Update(ctx context.Context, profile *model.TLSFingerprintProfile) (*model.TLSFingerprintProfile, error)
	Delete(ctx context.Context, id int64) error
}

// TLSFingerprintProfileCache 定义 TLS 指纹模板的缓存接口
type TLSFingerprintProfileCache interface {
	Get(ctx context.Context) ([]*model.TLSFingerprintProfile, bool)
	Set(ctx context.Context, profiles []*model.TLSFingerprintProfile) error
	Invalidate(ctx context.Context) error
	NotifyUpdate(ctx context.Context) error
	SubscribeUpdates(ctx context.Context, handler func())
}

// TLSFingerprintProfileService TLS 指纹模板管理服务
type TLSFingerprintProfileService struct {
	repo  TLSFingerprintProfileRepository
	cache TLSFingerprintProfileCache

	// 本地 ID→Profile 映射缓存，用于 DoWithTLS 热路径快速查找
	localCache map[int64]*model.TLSFingerprintProfile
	localMu    sync.RWMutex

	// randomByAccount 记住 id=-1（随机形态）账号抽到的形态。
	// 连接池缓存键包含形态身份（见 repository 的 getClientEntryWithTLS），
	// 若每次请求都重新随机，同一账号会不停地重建连接与重新握手；
	// 因此"随机"是每账号一次的选择，缓存刷新时清空。
	randomByAccount map[int64]*tlsfingerprint.Profile
	randomMu        sync.Mutex
}

// NewTLSFingerprintProfileService 创建 TLS 指纹模板服务
func NewTLSFingerprintProfileService(
	repo TLSFingerprintProfileRepository,
	cache TLSFingerprintProfileCache,
) *TLSFingerprintProfileService {
	svc := &TLSFingerprintProfileService{
		repo:       repo,
		cache:      cache,
		localCache: make(map[int64]*model.TLSFingerprintProfile),
	}

	ctx := context.Background()
	if err := svc.reloadFromDB(ctx); err != nil {
		logger.LegacyPrintf("service.tls_fp_profile", "[TLSFPProfileService] Failed to load profiles from DB on startup: %v", err)
		if fallbackErr := svc.refreshLocalCache(ctx); fallbackErr != nil {
			logger.LegacyPrintf("service.tls_fp_profile", "[TLSFPProfileService] Failed to load profiles from cache fallback on startup: %v", fallbackErr)
		}
	}

	if cache != nil {
		cache.SubscribeUpdates(ctx, func() {
			if err := svc.refreshLocalCache(context.Background()); err != nil {
				logger.LegacyPrintf("service.tls_fp_profile", "[TLSFPProfileService] Failed to refresh cache on notification: %v", err)
			}
		})
	}

	return svc
}

// --- CRUD ---

// List 获取所有模板
func (s *TLSFingerprintProfileService) List(ctx context.Context) ([]*model.TLSFingerprintProfile, error) {
	return s.repo.List(ctx)
}

// GetByID 根据 ID 获取模板
func (s *TLSFingerprintProfileService) GetByID(ctx context.Context, id int64) (*model.TLSFingerprintProfile, error) {
	return s.repo.GetByID(ctx, id)
}

// Create 创建模板
func (s *TLSFingerprintProfileService) Create(ctx context.Context, profile *model.TLSFingerprintProfile) (*model.TLSFingerprintProfile, error) {
	if err := profile.Validate(); err != nil {
		return nil, err
	}

	created, err := s.repo.Create(ctx, profile)
	if err != nil {
		return nil, err
	}

	refreshCtx, cancel := s.newCacheRefreshContext()
	defer cancel()
	s.invalidateAndNotify(refreshCtx)

	return created, nil
}

// Update 更新模板
func (s *TLSFingerprintProfileService) Update(ctx context.Context, profile *model.TLSFingerprintProfile) (*model.TLSFingerprintProfile, error) {
	if err := profile.Validate(); err != nil {
		return nil, err
	}

	updated, err := s.repo.Update(ctx, profile)
	if err != nil {
		return nil, err
	}

	refreshCtx, cancel := s.newCacheRefreshContext()
	defer cancel()
	s.invalidateAndNotify(refreshCtx)

	return updated, nil
}

// Delete 删除模板
func (s *TLSFingerprintProfileService) Delete(ctx context.Context, id int64) error {
	if err := s.repo.Delete(ctx, id); err != nil {
		return err
	}

	refreshCtx, cancel := s.newCacheRefreshContext()
	defer cancel()
	s.invalidateAndNotify(refreshCtx)

	return nil
}

// --- 热路径：运行时 Profile 查找 ---

// GetProfileByID 根据 ID 从本地缓存获取 Profile（用于 DoWithTLS 热路径）
// 返回 nil 表示未找到，调用方应 fallback 到内置默认 Profile
func (s *TLSFingerprintProfileService) GetProfileByID(id int64) *tlsfingerprint.Profile {
	s.localMu.RLock()
	p, ok := s.localCache[id]
	s.localMu.RUnlock()

	if ok && p != nil {
		return p.ToTLSProfile()
	}
	return nil
}

// getRandomProfile 从本地缓存中随机选择一个 Profile
func (s *TLSFingerprintProfileService) getRandomProfile() *tlsfingerprint.Profile {
	s.localMu.RLock()
	defer s.localMu.RUnlock()

	if len(s.localCache) == 0 {
		return nil
	}

	// 收集所有 profile
	profiles := make([]*model.TLSFingerprintProfile, 0, len(s.localCache))
	for _, p := range s.localCache {
		if p != nil {
			profiles = append(profiles, p)
		}
	}
	if len(profiles) == 0 {
		return nil
	}

	return profiles[rand.IntN(len(profiles))].ToTLSProfile()
}

// ResolveTLSProfile 根据 Account 的配置解析出运行时 TLS Profile
//
// 逻辑：
//  1. 未启用 TLS 指纹 → 返回 nil（不伪装）
//  2. 启用 + 绑定了 profile_id → 从缓存查找对应 profile
//  3. 启用 + 未绑定或找不到 → 按平台返回内置默认形态
//     - Anthropic：内置 Node.js 24.x（Claude Code）形态
//     - OpenAI OAuth：内置官方 codex CLI（Linux/OpenSSL）形态
func (s *TLSFingerprintProfileService) ResolveTLSProfile(account *Account) *tlsfingerprint.Profile {
	if account == nil || !account.IsTLSFingerprintEnabled() {
		return nil
	}
	id := account.GetTLSFingerprintProfileID()
	if id > 0 {
		if p := s.GetProfileByID(id); p != nil {
			return applyOpenAICodexHTTPShape(account, p)
		}
	}
	if id == -1 {
		// 随机形态只对 Anthropic 生效：表里混着各种平台的形态，给 OpenAI 账号随机抽一个
		// 反而会造出官方 codex 客户端不存在的 ClientHello。
		if account.IsOpenAIOAuth() {
			return tlsfingerprint.OpenAICodexLinuxProfile()
		}
		if p := s.getRandomProfileForAccount(account.ID); p != nil {
			return p
		}
	}
	// TLS 启用但无绑定 profile → 用按平台的内置默认形态（不落到别的平台的形态上）
	if account.IsOpenAIOAuth() {
		return tlsfingerprint.OpenAICodexLinuxProfile()
	}
	// Anthropic：空 Profile → dialer 使用内置默认值（Node.js 24.x / Claude Code）
	return &tlsfingerprint.Profile{Name: "Built-in Default (Node.js 24.x)"}
}

// applyOpenAICodexHTTPShape 给 OpenAI OAuth 账号用的形态补上“官方 HTTP 形态”：
// 请求头顺序表 + 关闭 Go 的自动 Accept-Encoding。
//
// 背景：tls_fingerprint_profiles 表只存 ClientHello 字段，
// internal/model 的 ToTLSProfile() 不映射这两个 HTTP 层字段（没有对应列）。
// 面板绑定 profile 后若不补，出站会退回 Go 的默认头序、并自动带上
// `Accept-Encoding: gzip`，正好丢掉两项已经和官方对齐的东西。
// 入参每次都是新构造的形态对象（ToTLSProfile 返回副本），这里再复制一层避免共享。
func applyOpenAICodexHTTPShape(account *Account, p *tlsfingerprint.Profile) *tlsfingerprint.Profile {
	if p == nil || account == nil || !account.IsOpenAIOAuth() {
		return p
	}
	clone := *p
	clone.DisableCompression = true
	if len(clone.HTTPHeaderOrders) == 0 {
		clone.HTTPHeaderOrders = tlsfingerprint.OpenAICodexLinuxProfile().HTTPHeaderOrders
	}
	return &clone
}

// getRandomProfileForAccount 为 id=-1 的账号抽取形态并**按账号固定**，理由见 randomByAccount。
func (s *TLSFingerprintProfileService) getRandomProfileForAccount(accountID int64) *tlsfingerprint.Profile {
	s.randomMu.Lock()
	defer s.randomMu.Unlock()
	if p, ok := s.randomByAccount[accountID]; ok && p != nil {
		return p
	}
	p := s.getRandomProfile()
	if p == nil {
		return nil
	}
	if s.randomByAccount == nil {
		s.randomByAccount = make(map[int64]*tlsfingerprint.Profile)
	}
	s.randomByAccount[accountID] = p
	return p
}

// --- 缓存管理 ---

func (s *TLSFingerprintProfileService) refreshLocalCache(ctx context.Context) error {
	if s.cache != nil {
		if profiles, ok := s.cache.Get(ctx); ok {
			s.setLocalCache(profiles)
			return nil
		}
	}
	return s.reloadFromDB(ctx)
}

func (s *TLSFingerprintProfileService) reloadFromDB(ctx context.Context) error {
	profiles, err := s.repo.List(ctx)
	if err != nil {
		return err
	}

	if s.cache != nil {
		if err := s.cache.Set(ctx, profiles); err != nil {
			logger.LegacyPrintf("service.tls_fp_profile", "[TLSFPProfileService] Failed to set cache: %v", err)
		}
	}

	s.setLocalCache(profiles)
	return nil
}

func (s *TLSFingerprintProfileService) setLocalCache(profiles []*model.TLSFingerprintProfile) {
	m := make(map[int64]*model.TLSFingerprintProfile, len(profiles))
	for _, p := range profiles {
		m[p.ID] = p
	}

	s.localMu.Lock()
	s.localCache = m
	s.localMu.Unlock()

	// 形态表变了，之前随机抽中的形态可能已被删除或改过，重新抽。
	s.randomMu.Lock()
	s.randomByAccount = nil
	s.randomMu.Unlock()
}

func (s *TLSFingerprintProfileService) newCacheRefreshContext() (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), 3*time.Second)
}

func (s *TLSFingerprintProfileService) invalidateAndNotify(ctx context.Context) {
	if s.cache != nil {
		if err := s.cache.Invalidate(ctx); err != nil {
			logger.LegacyPrintf("service.tls_fp_profile", "[TLSFPProfileService] Failed to invalidate cache: %v", err)
		}
	}

	if err := s.reloadFromDB(ctx); err != nil {
		logger.LegacyPrintf("service.tls_fp_profile", "[TLSFPProfileService] Failed to refresh local cache: %v", err)
		s.localMu.Lock()
		s.localCache = make(map[int64]*model.TLSFingerprintProfile)
		s.localMu.Unlock()
	}

	if s.cache != nil {
		if err := s.cache.NotifyUpdate(ctx); err != nil {
			logger.LegacyPrintf("service.tls_fp_profile", "[TLSFPProfileService] Failed to notify cache update: %v", err)
		}
	}
}
