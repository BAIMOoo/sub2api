package service

import (
	"encoding/json"
	"net/http"
	"strconv"

	"github.com/Wei-Shaw/sub2api/internal/pkg/proxyurl"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"golang.org/x/net/http/httpguts"
)

// CodexHTTPTransportConfigError reports a fixed HTTP contract that could not be
// applied. No upstream request was dispatched.
type CodexHTTPTransportConfigError struct {
	reason string
}

func (e *CodexHTTPTransportConfigError) Error() string {
	return "Codex HTTP configuration incompatible: " + e.reason
}

func (s *OpenAIGatewayService) doCodexHTTPUpstream(req *http.Request, proxyURL string, account *Account) (*http.Response, error) {
	if err := s.validateCodexHTTPTransport(req, proxyURL, account); err != nil {
		return nil, err
	}
	ctx := WithHTTPUpstreamProfile(req.Context(), HTTPUpstreamProfileCodexHTTP)
	return s.httpUpstream.DoWithTLS(req.WithContext(ctx), proxyURL, account.ID, account.Concurrency, tlsfingerprint.OpenAICodexLinuxProfile())
}

func (s *OpenAIGatewayService) validateCodexHTTPTransport(req *http.Request, proxyURL string, account *Account) error {
	if account == nil {
		return &CodexHTTPTransportConfigError{reason: "fixed transport requires a selected account"}
	}
	if req.Method != http.MethodPost || req.URL == nil || req.URL.String() != chatgptCodexURL || req.Host != "chatgpt.com" {
		return &CodexHTTPTransportConfigError{reason: "fixed transport requires the ChatGPT HTTPS endpoint"}
	}
	if req.Body == nil || req.GetBody == nil || req.ContentLength <= 0 ||
		req.Header.Get("Content-Encoding") != codexRequestBodyContentEncoding || len(req.TransferEncoding) != 0 {
		return &CodexHTTPTransportConfigError{reason: "fixed transport requires a replayable zstd request body"}
	}
	if enabled, configured := account.Extra["enable_tls_fingerprint"]; configured && enabled != true {
		return &CodexHTTPTransportConfigError{reason: "TLS fingerprint opt-out or invalid setting"}
	}
	if profile, configured := account.Extra["tls_fingerprint_profile_id"]; configured {
		switch profile {
		case 0, int64(0), float64(0), json.Number("0"): // default, not an override
		default:
			return &CodexHTTPTransportConfigError{reason: "custom or invalid TLS fingerprint profile"}
		}
	}
	_, proxy, err := proxyurl.Parse(proxyURL)
	if err != nil {
		return &CodexHTTPTransportConfigError{reason: "invalid proxy configuration"}
	}
	if proxy == nil && account.ProxyID != nil && *account.ProxyID > 0 {
		return &CodexHTTPTransportConfigError{reason: "configured proxy is unavailable"}
	}
	if proxy != nil {
		if proxy.Scheme == "https" {
			return &CodexHTTPTransportConfigError{reason: "HTTPS proxy cannot preserve the fixed TLS fingerprint"}
		}
		if port := proxy.Port(); port != "" {
			n, err := strconv.ParseUint(port, 10, 16)
			if err != nil || n == 0 {
				return &CodexHTTPTransportConfigError{reason: "invalid proxy port"}
			}
		}
	}
	if s.pluginManager != nil && s.pluginManager.ShouldRouteOpenAIOAuth(account) {
		return &CodexHTTPTransportConfigError{reason: "bound plugin transport is not verified for the fixed contract"}
	}
	return ValidateCodexHTTPHeaderBlock(req)
}

// ValidateCodexHTTPHeaderBlock also runs after the cookie jar adds its header.
// The general-purpose order writer passes through oversize blocks, which the
// fixed transport must never send.
func ValidateCodexHTTPHeaderBlock(req *http.Request) error {
	if !tlsfingerprint.FitsOrderedHTTPHeaderBlock(req) {
		return &CodexHTTPTransportConfigError{reason: "fixed request headers exceed the ordered transport limit"}
	}
	for name, values := range req.Header {
		if !httpguts.ValidHeaderFieldName(name) {
			return &CodexHTTPTransportConfigError{reason: "fixed request contains an invalid HTTP header name"}
		}
		for _, value := range values {
			if !httpguts.ValidHeaderFieldValue(value) {
				return &CodexHTTPTransportConfigError{reason: "fixed request contains an invalid HTTP header value"}
			}
		}
	}
	return nil
}
