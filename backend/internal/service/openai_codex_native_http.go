package service

import (
	"context"
	"net/http"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

const codexExecHTTPVersion = "0.158.0"

// Opaque non-Codex seeds have no original timestamp. Anchor their v7 mapping to
// this process lifetime, preserving equality across requests/accounts after the
// existing tenant/account scoping. A restart may change these synthetic IDs.
var codexHTTPIdentityEpoch = uuid.Must(uuid.NewV7()).String()

func normalizeCodexHTTPIdentityID(raw string) string {
	if _, ok := uuidV7TimestampMillis(raw); ok {
		return raw
	}
	value, _ := deriveStableCodexIdentityUUIDv7("sub2api:codex-http:v1:"+raw, codexHTTPIdentityEpoch)
	return value
}

func codexExecHTTPUserAgent() string {
	// Captured with the verified official linux/amd64 musl binary. This
	// environment is part of the fixed reference, not the gateway host's OS.
	return "codex_exec/0.158.0 (Alpine Linux 3.24.4; x86_64) unknown (codex_exec; 0.158.0)"
}

type codexHTTPAttemptKey struct{}

// The immutable pre-compression body is also the source of truth for indexed
// error recovery. Its presence marks an ordinary HTTP Codex attempt; WS bridges
// never receive this context value even though they share the request builders.
func withCodexHTTPAttempt(req *http.Request, c *gin.Context, account *Account, body []byte) *http.Request {
	if !codexHTTPPolicyForRequest(c, account).preservesCodexExec() {
		return req
	}
	return req.WithContext(context.WithValue(req.Context(), codexHTTPAttemptKey{}, body))
}

func codexHTTPAttemptBody(req *http.Request) ([]byte, bool) {
	body, ok := req.Context().Value(codexHTTPAttemptKey{}).([]byte)
	return body, ok
}

type codexHTTPPolicy uint8

const (
	codexHTTPLegacy codexHTTPPolicy = iota
	codexHTTPExec
)

func (policy codexHTTPPolicy) preservesCodexExec() bool {
	return policy == codexHTTPExec
}

// codexHTTPPolicyForRequest selects the outbound contract by ingress transport
// and upstream account, never by the caller's harness or client identity.
// An explicit HTTP marker keeps the entire WS ingress chain on its old policy,
// including bridges that eventually send an HTTP request upstream.
func codexHTTPPolicyForRequest(c *gin.Context, account *Account) codexHTTPPolicy {
	if c != nil && c.Request != nil && account != nil && account.IsOpenAIOAuthLike() &&
		GetOpenAIClientTransport(c) == OpenAIClientTransportHTTP {
		return codexHTTPExec
	}
	return codexHTTPLegacy
}

// Target HTTP requests use the pinned, approved exec contract.
// Legacy switches and per-account identity overrides only apply outside it.
func (s *OpenAIGatewayService) applyCodexHTTPIdentityHeaders(c *gin.Context, account *Account, h http.Header) {
	if h == nil {
		return
	}
	if codexHTTPPolicyForRequest(c, account).preservesCodexExec() {
		h.Set("user-agent", codexExecHTTPUserAgent())
		h.Set("originator", "codex_exec")
		h.Set("version", codexExecHTTPVersion)
		return
	}
	enforceCodexIdentityHeadersWithUA(h, s.codexIdentityOverrideUA(account))
}
