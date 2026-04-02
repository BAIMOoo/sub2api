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

	// 2a. Convert tools definition (skip server-side tools like web_search_*)
	for _, t := range anthropicReq.Tools {
		if strings.HasPrefix(t.Type, "web_search") {
			continue
		}
		ccReq.Tools = append(ccReq.Tools, apicompat.ChatTool{
			Type: "function",
			Function: &apicompat.ChatFunction{
				Name:        t.Name,
				Description: t.Description,
				Parameters:  t.InputSchema,
			},
		})
	}

	// 2b. Convert tool_choice
	if len(anthropicReq.ToolChoice) > 0 {
		var tc struct {
			Type string `json:"type"`
			Name string `json:"name,omitempty"`
		}
		if err := json.Unmarshal(anthropicReq.ToolChoice, &tc); err == nil {
			switch tc.Type {
			case "auto":
				ccReq.ToolChoice, _ = json.Marshal("auto")
			case "any":
				ccReq.ToolChoice, _ = json.Marshal("required")
			case "none":
				ccReq.ToolChoice, _ = json.Marshal("none")
			case "tool":
				ccReq.ToolChoice, _ = json.Marshal(map[string]any{
					"type": "function",
					"function": map[string]string{
						"name": tc.Name,
					},
				})
			}
		}
	}

	// 转换 system prompt（可能是字符串或 content block 数组）
	if anthropicReq.System != nil {
		var systemText string
		if err := json.Unmarshal(anthropicReq.System, &systemText); err != nil {
			// 尝试作为 content block 数组解析
			var blocks []apicompat.AnthropicContentBlock
			if err := json.Unmarshal(anthropicReq.System, &blocks); err == nil {
				var texts []string
				for _, b := range blocks {
					if b.Type == "text" && b.Text != "" {
						texts = append(texts, b.Text)
					}
				}
				systemText = strings.Join(texts, "\n")
			}
		}
		if systemText != "" {
			systemJSON, _ := json.Marshal(systemText)
			ccReq.Messages = append(ccReq.Messages, apicompat.ChatMessage{
				Role:    "system",
				Content: systemJSON,
			})
		}
	}

	// 转换 messages
	// Anthropic content 可能是字符串或 content block 数组 [{"type":"text","text":"..."},...]
	// Chat Completions 的 assistant content 必须是纯字符串，user content 可以是字符串或 parts 数组
	for _, msg := range anthropicReq.Messages {
		// 尝试提取纯文本：先看是否已经是字符串
		var plainText string
		if err := json.Unmarshal(msg.Content, &plainText); err == nil {
			// 已经是字符串，直接使用
			ccMsg := apicompat.ChatMessage{
				Role:    msg.Role,
				Content: msg.Content,
			}
			ccReq.Messages = append(ccReq.Messages, ccMsg)
			continue
		}

		// 是 content block 数组，需要根据 role 分别处理
		var blocks []apicompat.AnthropicContentBlock
		if err := json.Unmarshal(msg.Content, &blocks); err != nil {
			// 无法解析，原样传递
			ccReq.Messages = append(ccReq.Messages, apicompat.ChatMessage{
				Role:    msg.Role,
				Content: msg.Content,
			})
			continue
		}

		if msg.Role == "assistant" {
			// Assistant messages: extract text blocks + tool_use blocks
			ccMsg := apicompat.ChatMessage{Role: "assistant"}
			var texts []string
			for _, b := range blocks {
				switch b.Type {
				case "text":
					if b.Text != "" {
						texts = append(texts, b.Text)
					}
				case "tool_use":
					argsStr := string(b.Input)
					if argsStr == "" {
						argsStr = "{}"
					}
					ccMsg.ToolCalls = append(ccMsg.ToolCalls, apicompat.ChatToolCall{
						ID:   b.ID,
						Type: "function",
						Function: apicompat.ChatFunctionCall{
							Name:      b.Name,
							Arguments: argsStr,
						},
					})
				}
			}
			if len(texts) > 0 {
				ccMsg.Content, _ = json.Marshal(strings.Join(texts, "\n"))
			}
			ccReq.Messages = append(ccReq.Messages, ccMsg)
		} else {
			// User messages: separate text blocks and tool_result blocks
			var texts []string
			var toolResults []apicompat.AnthropicContentBlock
			for _, b := range blocks {
				switch b.Type {
				case "text":
					if b.Text != "" {
						texts = append(texts, b.Text)
					}
				case "tool_result":
					toolResults = append(toolResults, b)
				}
			}

			// Emit text blocks as a single user message
			if len(texts) > 0 {
				textContent, _ := json.Marshal(strings.Join(texts, "\n"))
				ccReq.Messages = append(ccReq.Messages, apicompat.ChatMessage{
					Role:    "user",
					Content: textContent,
				})
			}

			// Emit each tool_result as a separate tool role message
			for _, tr := range toolResults {
				resultText := extractToolResultText(tr.Content)
				resultJSON, _ := json.Marshal(resultText)
				ccReq.Messages = append(ccReq.Messages, apicompat.ChatMessage{
					Role:       "tool",
					Content:    resultJSON,
					ToolCallID: tr.ToolUseID,
				})
			}
		}
	}

	if anthropicReq.MaxTokens > 0 {
		maxTokens := anthropicReq.MaxTokens
		ccReq.MaxTokens = &maxTokens
	}

	// Forward optional sampling parameters
	if anthropicReq.Temperature != nil {
		ccReq.Temperature = anthropicReq.Temperature
	}
	if anthropicReq.TopP != nil {
		ccReq.TopP = anthropicReq.TopP
	}
	if len(anthropicReq.StopSeqs) > 0 {
		ccReq.Stop, _ = json.Marshal(anthropicReq.StopSeqs)
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

	// 5. Check and refresh API Key if expired
	expiresAt := account.GetCredentialAsInt64("expires_at")
	if expiresAt > 0 && time.Now().Unix() >= expiresAt {
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

	// 7. Check for HTTP errors before dispatching
	if resp.StatusCode >= 400 {
		respBody, _ := io.ReadAll(resp.Body)
		logger.LegacyPrintf("service.copilot", "Copilot API error: status=%d, body=%s", resp.StatusCode, string(respBody))
		return nil, fmt.Errorf("copilot API error: %d - %s", resp.StatusCode, string(respBody))
	}

	// 8. Handle response
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

	// Parse Chat Completions response
	var ccResp apicompat.ChatCompletionsResponse
	if err := json.Unmarshal(respBody, &ccResp); err != nil {
		return nil, fmt.Errorf("parse response: %w", err)
	}

	// Convert Chat Completions → Anthropic
	var stopReason string
	if len(ccResp.Choices) > 0 {
		stopReason = mapCCStopReason(ccResp.Choices[0].FinishReason)
	}
	var anthropicUsage apicompat.AnthropicUsage
	if ccResp.Usage != nil {
		anthropicUsage = apicompat.AnthropicUsage{
			InputTokens:  ccResp.Usage.PromptTokens,
			OutputTokens: ccResp.Usage.CompletionTokens,
		}
	}
	anthropicResp := &apicompat.AnthropicResponse{
		ID:         ccResp.ID,
		Type:       "message",
		Role:       "assistant",
		Model:      model,
		Content:    []apicompat.AnthropicContentBlock{},
		Usage:      anthropicUsage,
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

		// Convert tool_calls → tool_use content blocks
		for _, tc := range ccResp.Choices[0].Message.ToolCalls {
			args := json.RawMessage(tc.Function.Arguments)
			if len(args) == 0 || !json.Valid(args) {
				args = json.RawMessage("{}")
			}
			anthropicResp.Content = append(anthropicResp.Content, apicompat.AnthropicContentBlock{
				Type:  "tool_use",
				ID:    tc.ID,
				Name:  tc.Function.Name,
				Input: args,
			})
		}
	}

	c.JSON(http.StatusOK, anthropicResp)

	var resultUsage ClaudeUsage
	if ccResp.Usage != nil {
		resultUsage = ClaudeUsage{
			InputTokens:  ccResp.Usage.PromptTokens,
			OutputTokens: ccResp.Usage.CompletionTokens,
		}
	}
	return &ForwardResult{
		RequestID: ccResp.ID,
		Usage:     resultUsage,
		Model:     model,
		Stream:    false,
		Duration:  time.Since(startTime),
	}, nil
}

// copilotStreamState tracks the state machine for converting Chat Completions
// streaming chunks into the Anthropic SSE envelope protocol.
type copilotStreamState struct {
	messageStartSent bool
	blockIndex       int        // next block index to assign
	blockOpen        bool       // whether a content_block is currently open
	textBlockOpen    bool       // specifically if the open block is a text block
	toolCallMap      map[int]int // CC tool_call index -> Anthropic block index
}

func (s *GatewayService) handleCopilotStreamingAsMessages(
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
	// Increase scanner buffer for large chunks (1 MB)
	scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)

	var totalInput, totalOutput int
	requestID := ""

	state := &copilotStreamState{
		toolCallMap: make(map[int]int),
	}

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
			logger.L().Debug("copilot stream: failed to parse chunk", zap.Error(err))
			continue
		}

		if requestID == "" && chunk.ID != "" {
			requestID = chunk.ID
		}

		// Track usage from chunks (some providers send it on the final chunk)
		if chunk.Usage != nil {
			totalInput = chunk.Usage.PromptTokens
			totalOutput = chunk.Usage.CompletionTokens
		}

		// --- 1. Emit message_start on first chunk ---
		if !state.messageStartSent {
			msgID := requestID
			if msgID == "" {
				msgID = fmt.Sprintf("msg_%d", time.Now().UnixNano())
			}
			writeAnthropicSSE(w, "message_start", map[string]any{
				"type": "message_start",
				"message": map[string]any{
					"id":            msgID,
					"type":          "message",
					"role":          "assistant",
					"content":       []any{},
					"model":         model,
					"stop_reason":   nil,
					"stop_sequence": nil,
					"usage": map[string]int{
						"input_tokens":  0,
						"output_tokens": 0,
					},
				},
			})
			state.messageStartSent = true
		}

		if len(chunk.Choices) == 0 {
			continue
		}

		choice := chunk.Choices[0]
		delta := choice.Delta

		// --- 2. Handle tool_calls in delta ---
		for _, tc := range delta.ToolCalls {
			tcIndex := 0
			if tc.Index != nil {
				tcIndex = *tc.Index
			}

			if tc.ID != "" && tc.Function.Name != "" {
				// First chunk for a new tool call: has ID and function name.
				// Close any currently open text block first.
				if state.textBlockOpen {
					writeAnthropicSSE(w, "content_block_stop", map[string]any{
						"type":  "content_block_stop",
						"index": state.blockIndex - 1,
					})
					state.textBlockOpen = false
					state.blockOpen = false
				}

				// Emit content_block_start for tool_use
				blockIdx := state.blockIndex
				writeAnthropicSSE(w, "content_block_start", map[string]any{
					"type":  "content_block_start",
					"index": blockIdx,
					"content_block": map[string]any{
						"type":  "tool_use",
						"id":    tc.ID,
						"name":  tc.Function.Name,
						"input": map[string]any{},
					},
				})
				state.toolCallMap[tcIndex] = blockIdx
				state.blockIndex++
				state.blockOpen = true

				// If the first chunk also carries partial arguments, emit delta
				if tc.Function.Arguments != "" {
					writeAnthropicSSE(w, "content_block_delta", map[string]any{
						"type":  "content_block_delta",
						"index": blockIdx,
						"delta": map[string]string{
							"type":         "input_json_delta",
							"partial_json": tc.Function.Arguments,
						},
					})
				}
			} else if tc.Function.Arguments != "" {
				// Continuation chunk: only has partial arguments
				blockIdx, ok := state.toolCallMap[tcIndex]
				if !ok {
					// Shouldn't happen, but be defensive
					logger.L().Warn("copilot stream: tool_call continuation without start",
						zap.Int("tc_index", tcIndex))
					continue
				}
				writeAnthropicSSE(w, "content_block_delta", map[string]any{
					"type":  "content_block_delta",
					"index": blockIdx,
					"delta": map[string]string{
						"type":         "input_json_delta",
						"partial_json": tc.Function.Arguments,
					},
				})
			}
		}

		// --- 3. Handle text content deltas ---
		if delta.Content != nil && *delta.Content != "" {
			if !state.textBlockOpen {
				// Open a new text content block
				blockIdx := state.blockIndex
				writeAnthropicSSE(w, "content_block_start", map[string]any{
					"type":  "content_block_start",
					"index": blockIdx,
					"content_block": map[string]any{
						"type": "text",
						"text": "",
					},
				})
				state.blockIndex++
				state.textBlockOpen = true
				state.blockOpen = true
			}

			// Emit text delta (block index is always the current text block)
			writeAnthropicSSE(w, "content_block_delta", map[string]any{
				"type":  "content_block_delta",
				"index": state.blockIndex - 1,
				"delta": map[string]string{
					"type": "text_delta",
					"text": *delta.Content,
				},
			})
		}

		// --- 4. Handle finish ---
		if choice.FinishReason != nil {
			finishReason := *choice.FinishReason

			// Close any open text block
			if state.textBlockOpen {
				writeAnthropicSSE(w, "content_block_stop", map[string]any{
					"type":  "content_block_stop",
					"index": state.blockIndex - 1,
				})
				state.textBlockOpen = false
				state.blockOpen = false
			}

			// Close any open tool_use blocks
			for _, blockIdx := range state.toolCallMap {
				writeAnthropicSSE(w, "content_block_stop", map[string]any{
					"type":  "content_block_stop",
					"index": blockIdx,
				})
			}
			state.toolCallMap = make(map[int]int)
			state.blockOpen = false

			// Emit message_delta with stop_reason and usage
			outputTokens := totalOutput
			writeAnthropicSSE(w, "message_delta", map[string]any{
				"type": "message_delta",
				"delta": map[string]any{
					"stop_reason":   mapCCStopReason(finishReason),
					"stop_sequence": nil,
				},
				"usage": map[string]int{
					"output_tokens": outputTokens,
				},
			})

			// Emit message_stop
			writeAnthropicSSE(w, "message_stop", map[string]any{
				"type": "message_stop",
			})
		}
	}

	if err := scanner.Err(); err != nil {
		logger.L().Warn("copilot stream: scanner error",
			zap.Error(err),
			zap.String("request_id", requestID),
		)
	}

	// Safety: if the stream ended without a finish_reason (e.g. network error),
	// still close the envelope so the client doesn't hang.
	if state.messageStartSent {
		// Check if we never got a finish event
		needsClose := state.blockOpen || state.textBlockOpen
		if needsClose {
			if state.textBlockOpen {
				writeAnthropicSSE(w, "content_block_stop", map[string]any{
					"type":  "content_block_stop",
					"index": state.blockIndex - 1,
				})
			}
			for _, blockIdx := range state.toolCallMap {
				writeAnthropicSSE(w, "content_block_stop", map[string]any{
					"type":  "content_block_stop",
					"index": blockIdx,
				})
			}
			writeAnthropicSSE(w, "message_delta", map[string]any{
				"type": "message_delta",
				"delta": map[string]any{
					"stop_reason":   "end_turn",
					"stop_sequence": nil,
				},
				"usage": map[string]int{
					"output_tokens": totalOutput,
				},
			})
			writeAnthropicSSE(w, "message_stop", map[string]any{
				"type": "message_stop",
			})
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

// extractToolResultText extracts plain text from a tool_result content field.
// Content can be a JSON string or []AnthropicContentBlock.
func extractToolResultText(content json.RawMessage) string {
	if len(content) == 0 {
		return ""
	}
	var s string
	if err := json.Unmarshal(content, &s); err == nil {
		return s
	}
	var blocks []apicompat.AnthropicContentBlock
	if err := json.Unmarshal(content, &blocks); err == nil {
		var texts []string
		for _, b := range blocks {
			if b.Type == "text" && b.Text != "" {
				texts = append(texts, b.Text)
			}
		}
		return strings.Join(texts, "\n")
	}
	return string(content)
}

func writeAnthropicSSE(w gin.ResponseWriter, eventType string, data any) {
	j, _ := json.Marshal(data)
	fmt.Fprintf(w, "event: %s\ndata: %s\n\n", eventType, j)
	w.Flush()
}

func mapCCStopReason(finishReason string) string {
	switch finishReason {
	case "stop":
		return "end_turn"
	case "tool_calls":
		return "tool_use"
	case "length":
		return "max_tokens"
	case "content_filter":
		return "end_turn"
	default:
		return "end_turn"
	}
}
