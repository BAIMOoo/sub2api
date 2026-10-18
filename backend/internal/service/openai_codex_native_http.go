package service

import (
	"net/http"

	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	"github.com/gin-gonic/gin"
)

// nativeCodexHTTPIdentity selects preservation from the original HTTP headers,
// independently of ForceCodexCLI and the mutable outbound headers. Recognition
// is a wire-compatibility decision, not an authentication or access-control gate.
// WS and compatibility bridges retain their existing normalization contracts.
func nativeCodexHTTPIdentity(c *gin.Context, account *Account) (codexOutboundIdentity, bool) {
	if c == nil || c.Request == nil || account == nil || !account.UsesOpenAICodexProtocol() ||
		GetOpenAIClientTransport(c) != OpenAIClientTransportHTTP || isOpenAICompatMessagesBridgeContext(c) {
		return codexOutboundIdentity{}, false
	}
	h := c.Request.Header
	if len(h.Values("User-Agent")) != 1 || len(h.Values("originator")) > 1 || len(h.Values("version")) > 1 {
		return codexOutboundIdentity{}, false
	}
	ua := h.Get("User-Agent")
	originator, pairedUA, ok := openai.PairCodexClientIdentity(ua)
	if !ok || pairedUA != ua {
		return codexOutboundIdentity{}, false
	}
	version := NormalizeCodexClientVersion(openai.CodexUserAgentVersion(ua))
	if version == "" || CompareVersions(version, codexUpstreamMinVersion) < 0 ||
		openai.SetCodexUserAgentVersion(ua, version) != ua {
		return codexOutboundIdentity{}, false
	}
	if supplied := h.Get("originator"); supplied != "" && supplied != originator {
		return codexOutboundIdentity{}, false
	}
	if supplied := h.Get("version"); supplied != "" && supplied != version {
		return codexOutboundIdentity{}, false
	}
	return codexOutboundIdentity{userAgent: ua, originator: originator, version: version}, true
}

// applyCodexHTTPIdentityHeaders preserves a consistent native identity unless
// the administrator explicitly overrides it. Other outbound paths continue to
// use enforceCodexIdentityHeadersWithUA, including WS and credential requests.
func (s *OpenAIGatewayService) applyCodexHTTPIdentityHeaders(c *gin.Context, account *Account, h http.Header) {
	if h == nil || h.Get("originator") == "" {
		return
	}
	force := s != nil && s.cfg != nil && s.cfg.Gateway.ForceCodexCLI
	if identity, ok := nativeCodexHTTPIdentity(c, account); ok && !force && account.GetOpenAIUserAgent() == "" {
		h.Set("user-agent", identity.userAgent)
		h.Set("originator", identity.originator)
		h.Set("version", identity.version)
		return
	}
	enforceCodexIdentityHeadersWithUA(h, s.codexIdentityOverrideUA(account))
}
