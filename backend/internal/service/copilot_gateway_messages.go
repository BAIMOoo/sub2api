package service

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/apicompat"
	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
)

// ForwardCopilotAsMessages handles Copilot platform Anthropic Messages requests.
// Copilot uses Chat Completions API, so we convert Anthropic → CC → Copilot API → CC → Anthropic.
func (s *GatewayService) ForwardCopilotAsMessages(
	ctx context.Context,
	c *gin.Context,
	account *Account,
	body []byte,
	defaultMappedModel string,
) (*ForwardResult, error) {
	startTime := time.Now()

	// 1. Parse Anthropic request
	var anthropicReq apicompat.AnthropicRequest
	if err := json.Unmarshal(body, &anthropicReq); err != nil {
		return nil, fmt.Errorf("parse anthropic request: %w", err)
	}
	originalModel := anthropicReq.Model
	clientStream := anthropicReq.Stream

	// 2. Convert Anthropic → Chat Completions
	ccReq := &apicompat.ChatCompletionsRequest{
		Model:    anthropicReq.Model,
		Messages: []apicompat.ChatMessage{},
		Stream:   anthropicReq.Stream,
	}

	// Convert system prompt
	if anthropicReq.System != nil {
		var systemText string
		json.Unmarshal(anthropicReq.System, &systemText)
		if systemText != "" {
			systemJSON, _ := json.Marshal(systemText)
			ccReq.Messages = append(ccReq.Messages, apicompat.ChatMessage{
				Role:    "system",
				Content: systemJSON,
			})
		}
	}

	// Convert messages
	for _, msg := range anthropicReq.Messages {
		ccMsg := apicompat.ChatMessage{Role: msg.Role}
		var contentText string
		json.Unmarshal(msg.Content, &contentText)
		contentJSON, _ := json.Marshal(contentText)
		ccMsg.Content = contentJSON
		ccReq.Messages = append(ccReq.Messages, ccMsg)
	}

	if anthropicReq.MaxTokens > 0 {
		maxTokens := anthropicReq.MaxTokens
		ccReq.MaxTokens = &maxTokens
	}

	// 3. Model mapping
	mappedModel := resolveOpenAIForwardModel(account, originalModel, defaultMappedModel)
	ccReq.Model = mappedModel
	ccReq.Stream = clientStream

	logger.L().Debug("copilot messages: model mapping applied",
		zap.Int64("account_id", account.ID),
		zap.String("original_model", originalModel),
		zap.String("mapped_model", mappedModel),
		zap.Bool("stream", clientStream),
	)

	// 4. Marshal Chat Completions request
	ccBody, err := json.Marshal(ccReq)
	if err != nil {
		return nil, fmt.Errorf("marshal chat completions request: %w", err)
	}

	// 5. Build upstream request
	apiKey := account.GetCredential("api_key")
	if apiKey == "" {
		return nil, fmt.Errorf("copilot account has no API key")
	}

	upstreamURL := fmt.Sprintf("%s/chat/completions", copilotAPIBase)
	req, err := http.NewRequestWithContext(ctx, "POST", upstreamURL, bytes.NewReader(ccBody))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}

	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Copilot-Integration-Id", "vscode-chat")
	req.Header.Set("Editor-Version", "vscode/1.95.0")
	req.Header.Set("Editor-Plugin-Version", "copilot-chat/0.26.7")
	req.Header.Set("User-Agent", "GitHubCopilotChat/0.26.7")

	var proxyURL string
	if account.ProxyID != nil && account.Proxy != nil {
		proxyURL = account.Proxy.URL()
	}

	// 6. Send request
	resp, err := s.httpUpstream.DoWithTLS(req, proxyURL, account.ID, account.Concurrency, s.tlsFPProfileService.ResolveTLSProfile(account))
	if err != nil {
		return nil, fmt.Errorf("forward request: %w", err)
	}
	defer resp.Body.Close()

	// 7. Handle response
	if clientStream {
		return s.handleCopilotStreamingAsMessages(ctx, c, resp, originalModel, startTime)
	}
	return s.handleCopilotNonStreamingAsMessages(ctx, c, resp, originalModel, startTime)
}

func (s *GatewayService) handleCopilotNonStreamingAsMessages(
	ctx context.Context,
	c *gin.Context,
	resp *http.Response,
	model string,
	startTime time.Time,
) (*ForwardResult, error) {
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response: %w", err)
	}

	if resp.StatusCode >= 400 {
		logger.LegacyPrintf("service.copilot", "Copilot API error: status=%d, body=%s", resp.StatusCode, string(respBody))
		return nil, fmt.Errorf("copilot API error: %d - %s", resp.StatusCode, string(respBody))
	}

	// Parse Chat Completions response
	var ccResp apicompat.ChatCompletionsResponse
	if err := json.Unmarshal(respBody, &ccResp); err != nil {
		return nil, fmt.Errorf("parse response: %w", err)
	}

	// Convert Chat Completions → Anthropic
	var stopReason string
	if len(ccResp.Choices) > 0 {
		stopReason = ccResp.Choices[0].FinishReason
	}
	anthropicResp := &apicompat.AnthropicResponse{
		ID:      ccResp.ID,
		Type:    "message",
		Role:    "assistant",
		Model:   model,
		Content: []apicompat.AnthropicContentBlock{},
		Usage: apicompat.AnthropicUsage{
			InputTokens:  ccResp.Usage.PromptTokens,
			OutputTokens: ccResp.Usage.CompletionTokens,
		},
		StopReason: stopReason,
	}

	if len(ccResp.Choices) > 0 {
		var contentText string
		json.Unmarshal(ccResp.Choices[0].Message.Content, &contentText)
		if contentText != "" {
			anthropicResp.Content = append(anthropicResp.Content, apicompat.AnthropicContentBlock{
				Type: "text",
				Text: contentText,
			})
		}
	}

	c.JSON(http.StatusOK, anthropicResp)

	return &ForwardResult{
		RequestID: ccResp.ID,
		Usage: ClaudeUsage{
			InputTokens:  ccResp.Usage.PromptTokens,
			OutputTokens: ccResp.Usage.CompletionTokens,
		},
		Model:    model,
		Stream:   false,
		Duration: time.Since(startTime),
	}, nil
}

func (s *GatewayService) handleCopilotStreamingAsMessages(
	ctx context.Context,
	c *gin.Context,
	resp *http.Response,
	model string,
	startTime time.Time,
) (*ForwardResult, error) {
	c.Header("Content-Type", "text/event-stream")
	c.Status(http.StatusOK)

	scanner := bufio.NewScanner(resp.Body)
	var totalInput, totalOutput int
	requestID := ""

	for scanner.Scan() {
		line := scanner.Text()
		if !strings.HasPrefix(line, "data: ") {
			continue
		}

		data := strings.TrimPrefix(line, "data: ")
		if data == "[DONE]" {
			break
		}

		var chunk apicompat.ChatCompletionsChunk
		if err := json.Unmarshal([]byte(data), &chunk); err != nil {
			continue
		}

		if requestID == "" && chunk.ID != "" {
			requestID = chunk.ID
		}

		// Convert to Anthropic SSE format
		if len(chunk.Choices) > 0 && chunk.Choices[0].Delta.Content != nil && *chunk.Choices[0].Delta.Content != "" {
			event := map[string]any{
				"type":  "content_block_delta",
				"index": 0,
				"delta": map[string]string{
					"type": "text_delta",
					"text": *chunk.Choices[0].Delta.Content,
				},
			}
			eventJSON, _ := json.Marshal(event)
			fmt.Fprintf(c.Writer, "event: content_block_delta\ndata: %s\n\n", string(eventJSON))
			c.Writer.Flush()
		}

		if chunk.Usage != nil {
			totalInput = chunk.Usage.PromptTokens
			totalOutput = chunk.Usage.CompletionTokens
		}
	}

	return &ForwardResult{
		RequestID: requestID,
		Usage: ClaudeUsage{
			InputTokens:  totalInput,
			OutputTokens: totalOutput,
		},
		Model:    model,
		Stream:   true,
		Duration: time.Since(startTime),
	}, nil
}
