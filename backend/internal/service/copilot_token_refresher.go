package service

import (
	"context"
	"fmt"
	"time"
)

// CopilotTokenRefresher 处理 GitHub Copilot API Key 刷新
type CopilotTokenRefresher struct {
	copilotOAuthService *CopilotOAuthService
}

// NewCopilotTokenRefresher 创建 Copilot token 刷新器
func NewCopilotTokenRefresher(copilotOAuthService *CopilotOAuthService) *CopilotTokenRefresher {
	return &CopilotTokenRefresher{
		copilotOAuthService: copilotOAuthService,
	}
}

// CacheKey 返回用于分布式锁的缓存键
func (r *CopilotTokenRefresher) CacheKey(account *Account) string {
	return fmt.Sprintf("copilot_token_refresh:%d", account.ID)
}

// CanRefresh 检查是否能处理此账号
func (r *CopilotTokenRefresher) CanRefresh(account *Account) bool {
	return account.Platform == PlatformCopilot && account.Type == AccountTypeOAuth
}

// NeedsRefresh 检查 API Key 是否需要刷新
func (r *CopilotTokenRefresher) NeedsRefresh(account *Account, refreshWindow time.Duration) bool {
	expiresAt := account.GetCredentialAsInt64("expires_at")
	if expiresAt == 0 {
		return false
	}

	expiryTime := time.Unix(expiresAt, 0)
	return time.Now().Add(refreshWindow).After(expiryTime)
}

// Refresh 刷新 Copilot API Key
func (r *CopilotTokenRefresher) Refresh(ctx context.Context, account *Account) (map[string]any, error) {
	accessToken := account.GetCredential("access_token")
	if accessToken == "" {
		return nil, fmt.Errorf("no access_token in credentials")
	}

	// 使用 access_token 刷新 Copilot API Key
	result, err := r.copilotOAuthService.RefreshAPIKey(ctx, accessToken)
	if err != nil {
		// GitHub access_token 过期会返回 401，这是不可重试错误
		// 需要用户重新授权（Device Flow 不支持 refresh_token）
		return nil, fmt.Errorf("refresh copilot API key (access_token may be expired, please re-authorize): %w", err)
	}

	// 保留原有 credentials，只更新 api_key 和 expires_at
	credentials := account.Credentials
	if credentials == nil {
		credentials = make(map[string]any)
	}

	credentials["api_key"] = result.APIKey
	credentials["expires_at"] = result.ExpiresAt

	return credentials, nil
}
