package service

import (
	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/apicompat"
)

func configuredMinOutputTokens(cfg *config.Config) int {
	if cfg != nil && cfg.Gateway.MinOutputTokens >= 16 {
		return cfg.Gateway.MinOutputTokens
	}
	return apicompat.DefaultMinMaxOutputTokens
}

func chatCompletionsToResponses(cfg *config.Config, req *apicompat.ChatCompletionsRequest) (*apicompat.ResponsesRequest, error) {
	return apicompat.ChatCompletionsToResponsesWithMinOutputTokens(req, configuredMinOutputTokens(cfg))
}

func chatCompletionsToResponsesWithPolicy(cfg *config.Config, req *apicompat.ChatCompletionsRequest, policy codexHTTPPolicy) (*apicompat.ResponsesRequest, error) {
	if !policy.usesFixedExecContract() {
		return chatCompletionsToResponses(cfg, req)
	}
	return apicompat.ChatCompletionsToResponsesWithOptions(req, configuredMinOutputTokens(cfg), apicompat.ChatCompletionsToResponsesOptions{
		Mode: apicompat.ChatCompletionsConversionFixedCodexHTTP,
	})
}

func settingServiceConfig(settingService *SettingService) *config.Config {
	if settingService == nil {
		return nil
	}
	return settingService.cfg
}
