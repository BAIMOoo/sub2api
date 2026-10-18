package repository

import (
	"context"
	"net/http"
	"net/url"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/codexprobe"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/Wei-Shaw/sub2api/internal/service"
)

// A probe gets a fresh instance of the production transport, including its
// original proxy, uTLS profile and header ordering. Never share its observer or
// connections with customer traffic. Cookie handling remains in the decorator.
func (s *httpUpstreamService) doAlignmentProbe(req *http.Request, proxyURL string, accountID int64, profile *tlsfingerprint.Profile, p *codexprobe.Probe) (*http.Response, error) {
	if err := p.CheckTransport(req, accountID, proxyURL); err != nil {
		return nil, err
	}
	if err := s.validateRequestHost(req); err != nil {
		return nil, err
	}
	proxy, err := url.Parse(proxyURL)
	if err != nil || profile == nil || len(profile.HeaderOrders()) == 0 ||
		(proxy.Scheme != "socks5" && proxy.Scheme != "socks5h" && proxy.Scheme != "http") {
		_ = p.Emit("incomplete", map[string]any{"reason": "unsupported_probe_transport"})
		return nil, codexprobe.ErrProbe
	}
	if !strings.EqualFold(req.URL.Scheme, "https") {
		return nil, codexprobe.ErrProbe
	}
	settings := s.applyProfilePoolSettings(defaultPoolSettings(s.cfg), service.HTTPUpstreamProfileFromContext(req.Context()))
	transport, err := buildUpstreamTransportWithTLSFingerprint(settings, proxy, profile)
	if err != nil {
		return nil, codexprobe.ErrProbe
	}
	client := &http.Client{Transport: transport, CheckRedirect: func(_ *http.Request, _ []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	ctx, cancel := context.WithDeadline(req.Context(), p.Deadline())
	resp, err := doUpstreamRequest(client, req.WithContext(ctx))
	if err != nil {
		cancel()
		transport.CloseIdleConnections()
		_ = p.Emit("incomplete", map[string]any{"reason": "upstream_or_capture_failed"})
		return nil, err
	}
	_ = p.Emit("response", map[string]any{"status": resp.StatusCode, "path": req.URL.Path})
	resp.Body = wrapTrackedBody(resp.Body, func() {
		cancel()
		transport.CloseIdleConnections()
	})
	return resp, nil
}
