package service

import "github.com/gin-gonic/gin"

const openAIDeferPreOutputKeepaliveKey = "openai_defer_pre_output_keepalive"

type openAIHTTPKeepalivePolicy struct {
	checkedPool bool
	mayUseCodex bool
}

// DeferOpenAIHTTPPreOutputKeepalive protects the status until a Codex-capable
// request has semantic output. Continuations may check the pool before waiting
// so a proven API-key-only pool can keep its queue heartbeats.
func (s *OpenAIGatewayService) DeferOpenAIHTTPPreOutputKeepalive(c *gin.Context, checkPoolBeforeWait bool) bool {
	c.Set(openAIDeferPreOutputKeepaliveKey, &openAIHTTPKeepalivePolicy{mayUseCodex: true})
	return !checkPoolBeforeWait || s.openAIHTTPPreOutputKeepaliveDeferred(c, nil)
}

func (s *OpenAIGatewayService) openAIHTTPPreOutputKeepaliveDeferred(c *gin.Context, account *Account) bool {
	if c == nil {
		return false
	}
	raw, _ := c.Get(openAIDeferPreOutputKeepaliveKey)
	policy, ok := raw.(*openAIHTTPKeepalivePolicy)
	if !ok {
		return false
	}
	if codexHTTPPolicyForRequest(c, account).usesFixedExecContract() {
		return true
	}
	if !policy.checkedPool {
		policy.checkedPool = true
		apiKey := getAPIKeyFromContext(c)
		if apiKey == nil || s.accountRepo == nil {
			return true
		}
		// Include cooldowns: an account that cannot serve this attempt can
		// recover before failover, after a heartbeat would have committed 200.
		accounts, err := s.listConfiguredOpenAIAccounts(c.Request.Context(), apiKey.GroupID, PlatformOpenAI)
		if err == nil && len(accounts) > 0 {
			policy.mayUseCodex = false
			for i := range accounts {
				if codexHTTPPolicyForRequest(c, &accounts[i]).usesFixedExecContract() {
					policy.mayUseCodex = true
					break
				}
			}
		}
	}
	return policy.mayUseCodex
}
