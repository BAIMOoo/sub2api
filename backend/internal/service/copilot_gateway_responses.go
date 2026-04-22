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
	"github.com/google/uuid"
	"go.uber.org/zap"
)

// ForwardCopilotAsResponses handles Copilot platform Anthropic Messages requests
// by converting them to the OpenAI Responses API format and forwarding to the
// Copilot /responses endpoint. This is needed for models like gpt-5.1-codex
// that only support the Responses API.
func (s *GatewayService) ForwardCopilotAsResponses(
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

	// 2. Convert Anthropic → Responses API format
	respReq, err := apicompat.AnthropicToResponses(&anthropicReq)
	if err != nil {
		return nil, fmt.Errorf("convert anthropic to responses: %w", err)
	}

	// 3. Model mapping
	// Strip Anthropic date-version suffixes before lookup (same as messages path).
	copilotModel := normalizeCopilotModel(originalModel)
	mappedModel := resolveOpenAIForwardModel(account, copilotModel, defaultMappedModel)
	respReq.Model = mappedModel
	respReq.Stream = clientStream

	logger.L().Debug("copilot responses: model mapping applied",
		zap.Int64("account_id", account.ID),
		zap.String("original_model", originalModel),
		zap.String("copilot_model", copilotModel),
		zap.String("mapped_model", mappedModel),
		zap.Bool("stream", clientStream),
	)

	// 4. Marshal Responses request
	respBody, err := json.Marshal(respReq)
	if err != nil {
		return nil, fmt.Errorf("marshal responses request: %w", err)
	}

	// 5. Check and refresh API Key if expired（提前 5 秒视为过期，应对时钟偏差）
	expiresAt := account.GetCredentialAsInt64("expires_at")
	if expiresAt > 0 && time.Now().Unix() >= expiresAt-5 {
		logger.L().Info("Copilot API Key expired, refreshing", zap.Int64("account_id", account.ID))
		if err := s.refreshCopilotAPIKey(ctx, account); err != nil {
			return nil, fmt.Errorf("refresh expired API key: %w", err)
		}
	}

	// 6. Build upstream request
	apiKey := account.GetCredential("api_key")
	if apiKey == "" {
		return nil, fmt.Errorf("copilot account has no API key")
	}

	upstreamURL := fmt.Sprintf("%s/responses", copilotAPIBase)
	req, err := http.NewRequestWithContext(ctx, "POST", upstreamURL, bytes.NewReader(respBody))
	if err != nil {
		return nil, fmt.Errorf("create request: %w", err)
	}

	// Set Copilot-required headers
	req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", apiKey))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Copilot-Integration-Id", "vscode-chat")
	req.Header.Set("Editor-Version", "vscode/1.95.0")
	req.Header.Set("Editor-Plugin-Version", "copilot-chat/0.26.7")
	req.Header.Set("User-Agent", "GitHubCopilotChat/0.26.7")
	req.Header.Set("openai-intent", "conversation-panel")
	req.Header.Set("x-github-api-version", "2025-04-01")
	req.Header.Set("x-request-id", uuid.New().String())
	req.Header.Set("x-vscode-user-agent-library-version", "electron-fetch")
	req.Header.Set("X-Initiator", determineInitiator(anthropicReq.Messages))

	var proxyURL string
	if account.ProxyID != nil && account.Proxy != nil {
		proxyURL = account.Proxy.URL()
	}

	// 7. Send request
	resp, err := s.httpUpstream.DoWithTLS(req, proxyURL, account.ID, account.Concurrency, s.tlsFPProfileService.ResolveTLSProfile(account))
	if err != nil {
		if isCopilotRetryableNetworkError(err) {
			return nil, &UpstreamFailoverError{
				StatusCode:             http.StatusBadGateway,
				RetryableOnSameAccount: true,
			}
		}
		return nil, fmt.Errorf("forward request: %w", err)
	}
	defer resp.Body.Close()

	// 8. Check for HTTP errors
	if resp.StatusCode >= 400 {
		errBody, _ := io.ReadAll(resp.Body)
		// 对于不可重试的客户端错误，直接写 Anthropic 格式错误，阻止 failover。
		if resp.StatusCode == http.StatusBadRequest && c != nil {
			if nfwErr := writeCopilotNonRetryableError(c, errBody); nfwErr != nil {
				return nil, nfwErr
			}
		}
		logger.LegacyPrintf("service.copilot", "Copilot Responses API error: status=%d, body=%s", resp.StatusCode, string(errBody))
		return nil, &UpstreamFailoverError{
			StatusCode:             resp.StatusCode,
			ResponseBody:           errBody,
			ResponseHeaders:        resp.Header,
			RetryableOnSameAccount: resp.StatusCode == http.StatusRequestTimeout,
		}
	}

	// 9. Handle response
	if clientStream {
		return s.handleCopilotResponsesStreaming(ctx, c, resp, originalModel, startTime)
	}
	return s.handleCopilotResponsesNonStreaming(ctx, c, resp, originalModel, startTime)
}

// handleCopilotResponsesNonStreaming reads the complete Responses API response,
// converts it to Anthropic format, and writes the JSON response.
func (s *GatewayService) handleCopilotResponsesNonStreaming(
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

	var responsesResp apicompat.ResponsesResponse
	if err := json.Unmarshal(respBody, &responsesResp); err != nil {
		return nil, fmt.Errorf("parse responses response: %w", err)
	}

	// Convert Responses → Anthropic
	anthropicResp := apicompat.ResponsesToAnthropic(&responsesResp, model)

	c.JSON(http.StatusOK, anthropicResp)

	var resultUsage ClaudeUsage
	if responsesResp.Usage != nil {
		resultUsage = ClaudeUsage{
			InputTokens:  responsesResp.Usage.InputTokens,
			OutputTokens: responsesResp.Usage.OutputTokens,
		}
		if responsesResp.Usage.InputTokensDetails != nil {
			resultUsage.CacheReadInputTokens = responsesResp.Usage.InputTokensDetails.CachedTokens
		}
	}

	return &ForwardResult{
		RequestID: responsesResp.ID,
		Usage:     resultUsage,
		Model:     model,
		Stream:    false,
		Duration:  time.Since(startTime),
	}, nil
}

// handleCopilotResponsesStreaming reads Responses SSE events from the upstream,
// converts each event to Anthropic SSE events, and writes them to the client.
func (s *GatewayService) handleCopilotResponsesStreaming(
	ctx context.Context,
	c *gin.Context,
	resp *http.Response,
	model string,
	startTime time.Time,
) (*ForwardResult, error) {
	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	c.Status(http.StatusOK)

	w := c.Writer

	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)

	// Stream diagnostics
	var (
		lastChunkTime     = time.Now()
		chunkCount        = 0
		firstChunkLogged  = false
	)

	state := apicompat.NewResponsesEventToAnthropicState()
	state.Model = model

	requestID := ""

	for scanner.Scan() {
		line := scanner.Text()

		// Check if client context was canceled
		if ctx.Err() != nil {
			logger.L().Info("copilot responses stream: client canceled",
				zap.String("request_id", requestID),
				zap.Int("chunks_received", chunkCount),
				zap.Duration("stream_duration", time.Since(startTime)),
			)
			return nil, ctx.Err()
		}

		// Periodic idle warning
		idle := time.Since(lastChunkTime)
		if idle > 10*time.Second {
			logger.L().Warn("copilot responses stream: upstream idle > 10s",
				zap.String("request_id", requestID),
				zap.Duration("idle_duration", idle),
				zap.Int("chunks_so_far", chunkCount),
			)
		}
		lastChunkTime = time.Now()
		chunkCount++

		if !firstChunkLogged {
			logger.L().Info("copilot responses stream: first chunk received",
				zap.String("request_id", requestID),
				zap.Duration("ttfb", time.Since(startTime)),
			)
			firstChunkLogged = true
		}

		// Log every 50th chunk
		if chunkCount%50 == 0 {
			logger.L().Info("copilot responses stream: progress",
				zap.String("request_id", requestID),
				zap.Int("chunk_count", chunkCount),
				zap.Duration("stream_duration", time.Since(startTime)),
			)
		}

		if !strings.HasPrefix(line, "data: ") {
			continue
		}

		data := strings.TrimPrefix(line, "data: ")
		if data == "[DONE]" {
			break
		}

		var event apicompat.ResponsesStreamEvent
		if err := json.Unmarshal([]byte(data), &event); err != nil {
			logger.L().Debug("copilot responses stream: failed to parse event", zap.Error(err))
			continue
		}

		if requestID == "" && event.Response != nil && event.Response.ID != "" {
			requestID = event.Response.ID
		}

		// Convert Responses event → Anthropic events
		anthEvents := apicompat.ResponsesEventToAnthropicEvents(&event, state)
		for _, evt := range anthEvents {
			sse, err := apicompat.ResponsesAnthropicEventToSSE(evt)
			if err != nil {
				continue
			}
			fmt.Fprint(w, sse)
			w.Flush()
		}
	}

	if err := scanner.Err(); err != nil {
		logger.L().Warn("copilot responses stream: scanner error",
			zap.Error(err),
			zap.String("request_id", requestID),
		)
	}

	// Finalize: emit synthetic termination events if stream ended prematurely
	finalEvents := apicompat.FinalizeResponsesAnthropicStream(state)
	for _, evt := range finalEvents {
		sse, err := apicompat.ResponsesAnthropicEventToSSE(evt)
		if err != nil {
			continue
		}
		fmt.Fprint(w, sse)
		w.Flush()
	}

	return &ForwardResult{
		RequestID: requestID,
		Usage: ClaudeUsage{
			InputTokens:         state.InputTokens,
			OutputTokens:        state.OutputTokens,
			CacheReadInputTokens: state.CacheReadInputTokens,
		},
		Model:    model,
		Stream:   true,
		Duration: time.Since(startTime),
	}, nil
}
