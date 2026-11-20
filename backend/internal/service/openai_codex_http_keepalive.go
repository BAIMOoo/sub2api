package service

import (
	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
)

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
		groupID := AccountListGroupUngrouped
		if s.cfg != nil && s.cfg.RunMode == config.RunModeSimple {
			groupID = 0 // Simple mode can select any OpenAI account.
		} else if apiKey.GroupID != nil && *apiKey.GroupID > 0 {
			groupID = *apiKey.GroupID
		}
		// Account membership is stable across cooldown expiry and scheduled
		// recovery from error status. Neither can undo an already flushed ping.
		accounts, err := s.accountRepo.ListAllWithFilters(c.Request.Context(), PlatformOpenAI, "", "", "", groupID, "")
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
