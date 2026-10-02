//go:build unit

package repository

import (
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

// TestGetClientEntryWithTLSSeparatesProfiles 形态身份必须参与连接池缓存键：
// 面板改了绑定、或形态被编辑过之后，必须重建客户端，否则账号会一直用旧形态的
// ClientHello 出站（活跃账号的条目每次请求都会刷新 lastUsed，等不到空闲淘汰）。
func TestGetClientEntryWithTLSSeparatesProfiles(t *testing.T) {
	s, ok := NewHTTPUpstream(nil).(*httpUpstreamService)
	require.True(t, ok)

	profileA := tlsfingerprint.OpenAICodexLinuxProfile()
	profileB := &tlsfingerprint.Profile{Name: "other shape", DisableCompression: true}
	require.NotEqual(t, profileA.Identity(), profileB.Identity())

	first, err := s.getClientEntryWithTLS("", 7, 2, profileA, service.HTTPUpstreamProfileDefault, false, false)
	require.NoError(t, err)
	again, err := s.getClientEntryWithTLS("", 7, 2, profileA, service.HTTPUpstreamProfileDefault, false, false)
	require.NoError(t, err)
	require.Same(t, first, again, "同一形态应复用同一个客户端")

	other, err := s.getClientEntryWithTLS("", 7, 2, profileB, service.HTTPUpstreamProfileDefault, false, false)
	require.NoError(t, err)
	require.NotSame(t, first, other, "形态变了必须重建客户端")
}

// TestGetClientEntryWithTLSKeepsNilProfileKey 未启用指纹（profile=nil）时保持原有缓存键语义。
func TestGetClientEntryWithTLSKeepsNilProfileKey(t *testing.T) {
	s, ok := NewHTTPUpstream(nil).(*httpUpstreamService)
	require.True(t, ok)

	first, err := s.getClientEntryWithTLS("", 8, 2, nil, service.HTTPUpstreamProfileDefault, false, false)
	require.NoError(t, err)
	again, err := s.getClientEntryWithTLS("", 8, 2, nil, service.HTTPUpstreamProfileDefault, false, false)
	require.NoError(t, err)
	require.Same(t, first, again)
}
