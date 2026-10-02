//go:build unit

package service

import (
	"context"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/model"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

// stubTLSProfileRepo 是最小的形态仓库替身：只提供 List（构造时加载）。
type stubTLSProfileRepo struct {
	profiles []*model.TLSFingerprintProfile
}

func (s *stubTLSProfileRepo) List(context.Context) ([]*model.TLSFingerprintProfile, error) {
	return s.profiles, nil
}

func (s *stubTLSProfileRepo) GetByID(_ context.Context, id int64) (*model.TLSFingerprintProfile, error) {
	for _, p := range s.profiles {
		if p.ID == id {
			return p, nil
		}
	}
	return nil, nil
}

func (s *stubTLSProfileRepo) Create(context.Context, *model.TLSFingerprintProfile) (*model.TLSFingerprintProfile, error) {
	return nil, nil
}

func (s *stubTLSProfileRepo) Update(context.Context, *model.TLSFingerprintProfile) (*model.TLSFingerprintProfile, error) {
	return nil, nil
}

func (s *stubTLSProfileRepo) Delete(context.Context, int64) error { return nil }

func newTLSProfileServiceForTest(profiles ...*model.TLSFingerprintProfile) *TLSFingerprintProfileService {
	return NewTLSFingerprintProfileService(&stubTLSProfileRepo{profiles: profiles}, nil)
}

func tlsProfileAccount(platform, accountType string, extra map[string]any) *Account {
	return &Account{ID: 99, Platform: platform, Type: accountType, Extra: extra}
}

// 面板绑定了 DB 里的形态时，OpenAI OAuth 账号仍必须拿到"官方 HTTP 形态"
// （头序表 + 关闭 Go 自动附加的 Accept-Encoding）——存储表只存 ClientHello 字段。
func TestResolveTLSProfileOpenAIBoundProfileGetsOfficialHTTPShape(t *testing.T) {
	stored := &model.TLSFingerprintProfile{
		ID:           5,
		Name:         "Codex CLI (Linux, OpenSSL)",
		CipherSuites: []uint16{0x1301},
	}
	svc := newTLSProfileServiceForTest(stored)

	account := tlsProfileAccount(PlatformOpenAI, AccountTypeOAuth, map[string]any{
		"enable_tls_fingerprint":     true,
		"tls_fingerprint_profile_id": float64(5),
	})
	got := svc.ResolveTLSProfile(account)
	require.NotNil(t, got)
	assert.True(t, got.DisableCompression, "绑定形态也必须关掉自动 Accept-Encoding")
	require.Len(t, got.HTTPHeaderOrders, 1, "绑定形态也必须带官方头序表")
	assert.Equal(t, []uint16{0x1301}, got.CipherSuites, "ClientHello 字段来自绑定的形态")

	// 存储里的形态对象不能被就地改写：别的账号（尤其 Anthropic）拿到的副本必须干净。
	fresh := stored.ToTLSProfile()
	assert.False(t, fresh.DisableCompression)
	assert.Empty(t, fresh.HTTPHeaderOrders)
}

// id=-1（随机形态）只对 Anthropic 生效：给 OpenAI 账号随机会抽到表里其他平台的形态。
func TestResolveTLSProfileOpenAIRandomUsesBuiltinCodexProfile(t *testing.T) {
	stored := &model.TLSFingerprintProfile{ID: 1, Name: "Node.js 24.x"}
	svc := newTLSProfileServiceForTest(stored)

	account := tlsProfileAccount(PlatformOpenAI, AccountTypeOAuth, map[string]any{
		"enable_tls_fingerprint":     true,
		"tls_fingerprint_profile_id": float64(-1),
	})
	got := svc.ResolveTLSProfile(account)
	require.NotNil(t, got)
	assert.Equal(t, tlsfingerprint.OpenAICodexLinuxProfile().Name, got.Name)
	assert.True(t, got.DisableCompression)
}

// Anthropic 行为保持原样：随机形态仍从表里取、按账号固定，且不被套上 OpenAI 的 HTTP 形态。
func TestResolveTLSProfileAnthropicUnchanged(t *testing.T) {
	stored := &model.TLSFingerprintProfile{ID: 1, Name: "Node.js 24.x", CipherSuites: []uint16{0x1301, 0x1302}}
	svc := newTLSProfileServiceForTest(stored)

	random := tlsProfileAccount(PlatformAnthropic, AccountTypeOAuth, map[string]any{
		"enable_tls_fingerprint":     true,
		"tls_fingerprint_profile_id": float64(-1),
	})
	got := svc.ResolveTLSProfile(random)
	require.NotNil(t, got)
	assert.Equal(t, "Node.js 24.x", got.Name, "Anthropic 仍从表里取随机形态")
	assert.False(t, got.DisableCompression, "Anthropic 形态不应被改成 OpenAI 的 HTTP 形态")
	assert.Empty(t, got.HTTPHeaderOrders)
	assert.Same(t, got, svc.ResolveTLSProfile(random), "随机形态按账号固定，避免每次请求重建连接")

	unbound := tlsProfileAccount(PlatformAnthropic, AccountTypeOAuth, map[string]any{"enable_tls_fingerprint": true})
	assert.Equal(t, "Built-in Default (Node.js 24.x)", svc.ResolveTLSProfile(unbound).Name)

	off := tlsProfileAccount(PlatformAnthropic, AccountTypeOAuth, nil)
	assert.Nil(t, svc.ResolveTLSProfile(off))
}
