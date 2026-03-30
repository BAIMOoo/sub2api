package service

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/gin-gonic/gin"
)

const (
	copilotAPIBase = "https://api.githubcopilot.com"
)

func (s *GatewayService) ForwardCopilot(ctx context.Context, c *gin.Context, account *Account, parsed *ParsedRequest) (*ForwardResult, error) {
	startTime := time.Now()

	apiKey := account.GetCredential("api_key")
	if apiKey == "" {
		return nil, fmt.Errorf("copilot account has no API key")
	}

	// 构建上游 URL
	upstreamURL := fmt.Sprintf("%s/chat/completions", copilotAPIBase)

	// 准备请求体
	body := parsed.Body

	// 调试日志
	logger.LegacyPrintf("service.copilot", "[DEBUG] ForwardCopilot: body=%s", string(body))

	// 构建请求
	req, err := http.NewRequestWithContext(ctx, "POST", upstreamURL, bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}

	// 设置 Copilot 必需的 headers
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Copilot-Integration-Id", "vscode-chat")
	req.Header.Set("Editor-Version", "vscode/1.95.0")
	req.Header.Set("Editor-Plugin-Version", "copilot-chat/0.26.7")
	req.Header.Set("User-Agent", "GitHubCopilotChat/0.26.7")

	// 获取代理配置
	var proxyURL string
	if account.ProxyID != nil && account.Proxy != nil {
		proxyURL = account.Proxy.URL()
	}

	// 发送请求（使用代理和 TLS 指纹）
	resp, err := s.httpUpstream.DoWithTLS(req, proxyURL, account.ID, account.Concurrency, s.tlsFPProfileService.ResolveTLSProfile(account))
	if err != nil {
		return nil, fmt.Errorf("forward request: %w", err)
	}
	defer resp.Body.Close()

	// 读取响应
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}

	// 处理错误响应
	if resp.StatusCode >= 400 {
		logger.LegacyPrintf("service.copilot", "Copilot API error: status=%d, body=%s, request_body=%s", resp.StatusCode, string(respBody), string(body))
		return nil, fmt.Errorf("copilot API error: %d - %s", resp.StatusCode, string(respBody))
	}

	// 解析响应以提取 token 使用量
	var respData struct {
		Usage struct {
			PromptTokens     int `json:"prompt_tokens"`
			CompletionTokens int `json:"completion_tokens"`
			TotalTokens      int `json:"total_tokens"`
		} `json:"usage"`
	}

	usage := ClaudeUsage{}
	if err := json.Unmarshal(respBody, &respData); err == nil {
		usage.InputTokens = respData.Usage.PromptTokens
		usage.OutputTokens = respData.Usage.CompletionTokens
		logger.LegacyPrintf("service.copilot", "Copilot usage: prompt=%d, completion=%d, total=%d",
			respData.Usage.PromptTokens, respData.Usage.CompletionTokens, respData.Usage.TotalTokens)
	}

	// 将响应写回客户端
	if c != nil {
		c.Data(resp.StatusCode, "application/json", respBody)
	}

	return &ForwardResult{
		RequestID:    resp.Header.Get("x-request-id"),
		Usage:        usage,
		Model:        parsed.Model,
		Stream:       parsed.Stream,
		Duration:     time.Since(startTime),
	}, nil
}
