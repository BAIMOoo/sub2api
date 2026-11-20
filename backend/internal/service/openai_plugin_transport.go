package service

import (
	"context"
	"net/http"

	"github.com/Wei-Shaw/sub2api/internal/pkg/codexprobe"
)

func checkAlignmentProbeAccount(ctx context.Context, account *Account) error {
	if codexprobe.FromContext(ctx) == nil {
		return nil
	}
	if account == nil || account.IsShadow() {
		return codexprobe.ErrProbe
	}
	var proxyID int64
	if account.ProxyID != nil {
		proxyID = *account.ProxyID
	}
	return codexprobe.CheckSelection(ctx, account.ID, proxyID, account.IsOpenAIOAuthLike())
}

func (s *OpenAIGatewayService) SetPluginManager(manager *PluginManager) {
	s.pluginManager = manager
}

type openAIWSHTTPBridgeUpstreamKey struct{}

// IsOpenAIWSHTTPBridgeUpstream identifies the bridge's last hop, independently of
// the provider's HTTP profile. Only the bridge marks it, never a client header.
func IsOpenAIWSHTTPBridgeUpstream(ctx context.Context) bool {
	return ctx != nil && ctx.Value(openAIWSHTTPBridgeUpstreamKey{}) == true
}

func (s *OpenAIGatewayService) doOpenAIWSHTTPBridgeUpstream(request *http.Request, proxyURL string, account *Account) (*http.Response, error) {
	// Scope the marker to this attempt; credential lookups and subsequent HTTP requests do not inherit it.
	ctx := context.WithValue(request.Context(), openAIWSHTTPBridgeUpstreamKey{}, true)
	return s.doOpenAIUpstream(request.WithContext(ctx), proxyURL, account)
}

// doOpenAIUpstream 只在 OpenAI OAuth 能力绑定已启用时把真实请求交给插件。
// 插件返回标准 http.Response，响应解析、错误映射、SSE 和计费仍由现有核心链处理。
func (s *OpenAIGatewayService) doOpenAIUpstream(request *http.Request, proxyURL string, account *Account) (*http.Response, error) {
	if p := codexprobe.FromContext(request.Context()); p != nil {
		if err := checkAlignmentProbeAccount(request.Context(), account); err != nil {
			return nil, err
		}
		if err := p.CheckTransport(request, account.ID, proxyURL); err != nil {
			return nil, err
		}
		if s.pluginManager != nil && s.pluginManager.ShouldRouteOpenAIOAuth(account) {
			_ = p.Emit("incomplete", map[string]any{"reason": "plugin_transport_not_observable"})
			return nil, codexprobe.ErrProbe
		}
	}
	if _, fixed := codexHTTPAttemptBody(request); fixed && !IsOpenAIWSHTTPBridgeUpstream(request.Context()) {
		return s.doCodexHTTPUpstream(request, proxyURL, account)
	}
	if s.pluginManager != nil {
		response, handled, err := s.pluginManager.RoundTripOpenAIOAuth(request.Context(), request, proxyURL, account)
		if handled {
			return response, err
		}
	}
	// Only ordinary HTTP uses the fork's account TLS override. A bridged WS turn
	// keeps upstream's plugin-first, plain Do fallback, even on the same account.
	if s.tlsFPProfileService != nil && !IsOpenAIWSHTTPBridgeUpstream(request.Context()) {
		if profile := s.tlsFPProfileService.ResolveTLSProfile(account); profile != nil {
			return s.httpUpstream.DoWithTLS(request, proxyURL, account.ID, account.Concurrency, profile)
		}
	}
	return s.httpUpstream.Do(request, proxyURL, account.ID, account.Concurrency)
}

// doOpenAIAccountTestUpstream 让 OpenAI OAuth 账号测试与真实转发使用同一插件路径。
// API Key 和未命中插件的账号保持各自原有的 HTTPUpstream 行为。
func (s *AccountTestService) doOpenAIAccountTestUpstream(
	request *http.Request,
	proxyURL string,
	account *Account,
	useTLSFallback bool,
) (*http.Response, error) {
	if s.pluginManager != nil {
		response, handled, err := s.pluginManager.RoundTripOpenAIOAuth(request.Context(), request, proxyURL, account)
		if handled {
			return response, err
		}
	}
	if useTLSFallback {
		return s.httpUpstream.DoWithTLS(
			request,
			proxyURL,
			account.ID,
			account.Concurrency,
			s.tlsFPProfileService.ResolveTLSProfile(account),
		)
	}
	return s.httpUpstream.Do(request, proxyURL, account.ID, account.Concurrency)
}
