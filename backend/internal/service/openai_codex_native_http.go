package service

import (
	"context"
	"net/http"

	"github.com/gin-gonic/gin"
)

const (
	codexExecHTTPVersion        = "0.171.0"
	codexExecHTTPAcceptEncoding = "zstd,gzip"
)

type codexHTTPPolicy uint8

const (
	codexHTTPLegacy codexHTTPPolicy = iota
	codexHTTPExec
)

func (policy codexHTTPPolicy) usesFixedExecContract() bool {
	return policy == codexHTTPExec
}

func codexExecHTTPUserAgent() string {
	return "codex_exec/0.171.0 (Alpine Linux 3.24.4; x86_64) unknown (codex_exec; 0.171.0)"
}

const codexHTTPIdentityEpoch = "01a00000-0000-7000-8000-000000000000"

func normalizeCodexHTTPIdentityID(raw string) string {
	if _, ok := uuidV7TimestampMillis(raw); ok {
		return raw
	}
	value, _ := deriveStableCodexIdentityUUIDv7("sub2api:codex-http:v1:"+raw, codexHTTPIdentityEpoch)
	return value
}

type codexHTTPAttemptKey struct{}

func (policy codexHTTPPolicy) withAttemptBody(req *http.Request, body []byte) *http.Request {
	if !policy.usesFixedExecContract() {
		return req
	}
	ctx := WithHTTPUpstreamRedirectsDisabled(req.Context())
	return req.WithContext(context.WithValue(ctx, codexHTTPAttemptKey{}, body))
}

func codexHTTPAttemptBody(req *http.Request) ([]byte, bool) {
	body, ok := req.Context().Value(codexHTTPAttemptKey{}).([]byte)
	return body, ok
}

// codexHTTPPolicyForRequest selects the fixed HTTP contract from the ingress
// transport and final Codex account, never from a caller-controlled identity.
func codexHTTPPolicyForRequest(c *gin.Context, account *Account) codexHTTPPolicy {
	implicitOpenAIOAuth := account != nil && account.Platform == "" && account.Type == AccountTypeOAuth
	if c != nil && account != nil && (account.IsOpenAIOAuthLike() || implicitOpenAIOAuth) &&
		GetOpenAIClientTransport(c) == OpenAIClientTransportHTTP {
		return codexHTTPExec
	}
	return codexHTTPLegacy
}

// Direct HTTP Codex requests always use the approved server-owned identity.
// WebSocket and other upstream paths continue through the legacy policy.
func (s *OpenAIGatewayService) applyCodexHTTPIdentityHeaders(c *gin.Context, account *Account, h http.Header) {
	if h == nil {
		return
	}
	if codexHTTPPolicyForRequest(c, account).usesFixedExecContract() {
		h.Set("user-agent", codexExecHTTPUserAgent())
		h.Set("originator", "codex_exec")
		h.Set("version", codexExecHTTPVersion)
		return
	}
	if h.Get("originator") == "" {
		return
	}
	enforceCodexIdentityHeadersWithUA(h, s.codexIdentityOverrideUA(account))
}
