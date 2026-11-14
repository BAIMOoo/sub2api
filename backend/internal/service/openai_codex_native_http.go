package service

import (
	"net/http"

	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	"github.com/gin-gonic/gin"
)

type codexHTTPPolicy uint8

const (
	codexHTTPLegacy codexHTTPPolicy = iota
	codexHTTPExec
)

func (policy codexHTTPPolicy) preservesCodexExec() bool {
	return policy == codexHTTPExec
}

func parseNativeCodexHTTPClientIdentity(c *gin.Context, account *Account) (codexOutboundIdentity, string, bool) {
	if c == nil || c.Request == nil || account == nil || !account.UsesOpenAICodexProtocol() ||
		GetOpenAIClientTransport(c) != OpenAIClientTransportHTTP || isOpenAICompatMessagesBridgeContext(c) {
		return codexOutboundIdentity{}, "", false
	}
	h := c.Request.Header
	if len(h.Values("User-Agent")) != 1 || len(h.Values("originator")) > 1 || len(h.Values("version")) > 1 {
		return codexOutboundIdentity{}, "", false
	}
	ua := h.Get("User-Agent")
	originator, pairedUA, ok := openai.PairCodexClientIdentity(ua)
	if !ok {
		return codexOutboundIdentity{}, "", false
	}
	version := NormalizeCodexClientVersion(openai.CodexUserAgentVersion(ua))
	if version == "" || CompareVersions(version, codexUpstreamMinVersion) < 0 ||
		openai.SetCodexUserAgentVersion(ua, version) != ua {
		return codexOutboundIdentity{}, "", false
	}
	return codexOutboundIdentity{userAgent: ua, originator: originator, version: version}, pairedUA, true
}

// nativeCodexHTTPClientIdentity recognizes a Codex harness from the original
// HTTP User-Agent independently of ForceCodexCLI and the mutable outbound
// headers. It intentionally ignores conflicting originator/version headers:
// recognition selects the exec body contract, while outbound enforcement will
// replace those headers with the canonical codex_exec identity.
func nativeCodexHTTPClientIdentity(c *gin.Context, account *Account) (codexOutboundIdentity, bool) {
	identity, _, ok := parseNativeCodexHTTPClientIdentity(c, account)
	return identity, ok
}

// nativeCodexHTTPIdentity additionally requires consistent originator/version
// headers. Only an exact, internally consistent codex_exec identity may be
// preserved verbatim.
func nativeCodexHTTPIdentity(c *gin.Context, account *Account) (codexOutboundIdentity, bool) {
	identity, pairedUA, ok := parseNativeCodexHTTPClientIdentity(c, account)
	if !ok {
		return codexOutboundIdentity{}, false
	}
	h := c.Request.Header
	if pairedUA != h.Get("User-Agent") {
		return codexOutboundIdentity{}, false
	}
	originator, version := identity.originator, identity.version
	if supplied := h.Get("originator"); supplied != "" && supplied != originator {
		return codexOutboundIdentity{}, false
	}
	if supplied := h.Get("version"); supplied != "" && supplied != version {
		return codexOutboundIdentity{}, false
	}
	return identity, true
}

func nativeCodexHTTPPolicy(c *gin.Context, account *Account) codexHTTPPolicy {
	if _, ok := nativeCodexHTTPClientIdentity(c, account); ok {
		return codexHTTPExec
	}
	return codexHTTPLegacy
}

// applyCodexHTTPIdentityHeaders preserves a consistent native identity unless
// the administrator explicitly overrides it. Other outbound paths continue to
// use enforceCodexIdentityHeadersWithUA, including WS and credential requests.
func (s *OpenAIGatewayService) applyCodexHTTPIdentityHeaders(c *gin.Context, account *Account, h http.Header) {
	if h == nil || h.Get("originator") == "" {
		return
	}
	force := s != nil && s.cfg != nil && s.cfg.Gateway.ForceCodexCLI
	if identity, ok := nativeCodexHTTPIdentity(c, account); ok && identity.originator == "codex_exec" &&
		!force && account.GetOpenAIUserAgent() == "" {
		h.Set("user-agent", identity.userAgent)
		h.Set("originator", identity.originator)
		h.Set("version", identity.version)
		return
	}
	enforceCodexIdentityHeadersWithUA(h, s.codexIdentityOverrideUA(account))
}
