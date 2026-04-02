package service

import (
	"context"
	"fmt"

	"github.com/Wei-Shaw/sub2api/internal/pkg/copilot"
	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"go.uber.org/zap"
)

type CopilotOAuthService struct {
	accountRepo AccountRepository
}

func NewCopilotOAuthService(accountRepo AccountRepository) *CopilotOAuthService {
	return &CopilotOAuthService{
		accountRepo: accountRepo,
	}
}

type CopilotAuthURLResult struct {
	DeviceCode      string `json:"device_code"`
	UserCode        string `json:"user_code"`
	VerificationURI string `json:"verification_uri"`
	ExpiresIn       int    `json:"expires_in"`
	Interval        int    `json:"interval"`
}

func (s *CopilotOAuthService) InitiateDeviceFlow(ctx context.Context) (*CopilotAuthURLResult, error) {
	deviceCode, err := copilot.GetDeviceCode(ctx)
	if err != nil {
		logger.L().Error("Failed to get device code", zap.Error(err))
		return nil, fmt.Errorf("failed to initiate device flow: %w", err)
	}

	return &CopilotAuthURLResult{
		DeviceCode:      deviceCode.DeviceCode,
		UserCode:        deviceCode.UserCode,
		VerificationURI: deviceCode.VerificationURI,
		ExpiresIn:       deviceCode.ExpiresIn,
		Interval:        deviceCode.Interval,
	}, nil
}

type CopilotCallbackResult struct {
	AccessToken string `json:"access_token"`
	APIKey      string `json:"api_key"`
	ExpiresAt   int64  `json:"expires_at"`
}

func (s *CopilotOAuthService) CompleteDeviceFlow(ctx context.Context, deviceCode string, interval int) (*CopilotCallbackResult, error) {
	accessToken, err := copilot.PollForAccessToken(ctx, deviceCode, interval)
	if err != nil {
		logger.L().Error("Failed to get access token", zap.Error(err))
		return nil, fmt.Errorf("failed to complete device flow: %w", err)
	}

	apiKeyResp, err := copilot.GetAPIKey(ctx, accessToken)
	if err != nil {
		logger.L().Error("Failed to get API key", zap.Error(err))
		return nil, fmt.Errorf("failed to get API key: %w", err)
	}

	return &CopilotCallbackResult{
		AccessToken: accessToken,
		APIKey:      apiKeyResp.Token,
		ExpiresAt:   apiKeyResp.ExpiresAt,
	}, nil
}

func (s *CopilotOAuthService) RefreshAPIKey(ctx context.Context, accessToken string) (*CopilotCallbackResult, error) {
	apiKeyResp, err := copilot.GetAPIKey(ctx, accessToken)
	if err != nil {
		logger.L().Error("Failed to refresh API key", zap.Error(err))
		return nil, fmt.Errorf("failed to refresh API key: %w", err)
	}

	return &CopilotCallbackResult{
		AccessToken: accessToken,
		APIKey:      apiKeyResp.Token,
		ExpiresAt:   apiKeyResp.ExpiresAt,
	}, nil
}
