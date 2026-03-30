# Copilot Gateway Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decouple Copilot from OpenAI Gateway into an independent path through main GatewayService.

**Architecture:** Copilot currently piggybacks on OpenAI Gateway via scattered `PlatformCopilot` checks. We'll migrate the Copilot Messages conversion logic to GatewayService, add a Copilot branch to GatewayHandler.Messages, update routing, and revert all OpenAI Gateway hacks.

**Tech Stack:** Go, Gin HTTP framework, Wire DI

**Spec:** `docs/superpowers/specs/2026-03-31-copilot-gateway-refactor-design.md`

---

### Task 1: Create copilot_gateway_messages.go (migrate ForwardCopilotAsMessages)

Migrate `forwardCopilotAsAnthropic` from `OpenAIGatewayService` to `GatewayService`, changing the return type from `*OpenAIForwardResult` to `*ForwardResult`.

**Files:**
- Create: `backend/internal/service/copilot_gateway_messages.go`
- Reference: `backend/internal/service/openai_copilot_messages.go` (source to migrate)
- Reference: `backend/internal/service/copilot_gateway_service.go` (existing ForwardCopilot pattern)

- [ ] **Step 1: Create copilot_gateway_messages.go with ForwardCopilotAsMessages**

Create `backend/internal/service/copilot_gateway_messages.go` with the following content. This is a migration of `openai_copilot_messages.go` with these changes:
1. Method receiver: `*OpenAIGatewayService` → `*GatewayService`
2. Method name: `forwardCopilotAsAnthropic` → `ForwardCopilotAsMessages` (public)
3. Return type: `*OpenAIForwardResult` → `*ForwardResult`
4. Usage type: `OpenAIUsage` → `ClaudeUsage`
5. HTTP client: `s.httpUpstream.Do(...)` → `s.httpUpstream.DoWithTLS(...)` (consistent with existing `ForwardCopilot`)
6. Helper methods: receiver changed from `*OpenAIGatewayService` to `*GatewayService`

```go
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
		StopReason: ccResp.Choices[0].FinishReason,
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
```

- [ ] **Step 2: Verify the file compiles**

Run:
```bash
cd backend && go build ./internal/service/
```
Expected: no errors (the new methods are defined but not yet called)

- [ ] **Step 3: Commit**

```bash
git add backend/internal/service/copilot_gateway_messages.go
git commit -m "feat(copilot): add ForwardCopilotAsMessages on GatewayService

Migrate Copilot Anthropic Messages conversion from OpenAIGatewayService
to GatewayService as the first step in decoupling Copilot from OpenAI Gateway."
```

---

### Task 2: Add Copilot branch to GatewayHandler.Messages

Add a Copilot platform branch in `GatewayHandler.Messages()`, before the Gemini branch. This follows the same failover pattern as Gemini.

**Files:**
- Modify: `backend/internal/handler/gateway_handler.go:284` (insert Copilot branch before Gemini branch)

- [ ] **Step 1: Add Copilot branch before Gemini branch**

In `backend/internal/handler/gateway_handler.go`, insert the following Copilot branch **immediately before** `if platform == service.PlatformGemini {` (around line 284):

```go
	if platform == service.PlatformCopilot {
		fs := NewFailoverState(h.maxAccountSwitches, hasBoundSession)

		for {
			selection, err := h.gatewayService.SelectAccountWithLoadAwareness(c.Request.Context(), apiKey.GroupID, sessionKey, reqModel, fs.FailedAccountIDs, "")
			if err != nil {
				if len(fs.FailedAccountIDs) == 0 {
					h.handleStreamingAwareError(c, http.StatusServiceUnavailable, "api_error", "No available accounts: "+err.Error(), streamStarted)
					return
				}
				action := fs.HandleSelectionExhausted(c.Request.Context())
				switch action {
				case FailoverContinue:
					continue
				case FailoverCanceled:
					return
				default:
					if fs.LastFailoverErr != nil {
						h.handleFailoverExhausted(c, fs.LastFailoverErr, service.PlatformCopilot, streamStarted)
					} else {
						h.handleFailoverExhaustedSimple(c, 502, streamStarted)
					}
					return
				}
			}
			account := selection.Account
			setOpsSelectedAccount(c, account.ID, account.Platform)

			// Acquire account concurrency slot
			accountReleaseFunc := selection.ReleaseFunc
			if !selection.Acquired {
				if selection.WaitPlan == nil {
					h.handleStreamingAwareError(c, http.StatusServiceUnavailable, "api_error", "No available accounts", streamStarted)
					return
				}
				accountWaitCounted := false
				canWait, err := h.concurrencyHelper.IncrementAccountWaitCount(c.Request.Context(), account.ID, selection.WaitPlan.MaxWaiting)
				if err != nil {
					reqLog.Warn("gateway.account_wait_counter_increment_failed", zap.Int64("account_id", account.ID), zap.Error(err))
				} else if !canWait {
					reqLog.Info("gateway.account_wait_queue_full",
						zap.Int64("account_id", account.ID),
						zap.Int("max_waiting", selection.WaitPlan.MaxWaiting),
					)
					h.handleStreamingAwareError(c, http.StatusTooManyRequests, "rate_limit_error", "Too many pending requests, please retry later", streamStarted)
					return
				}
				if err == nil && canWait {
					accountWaitCounted = true
				}
				releaseWait := func() {
					if accountWaitCounted {
						h.concurrencyHelper.DecrementAccountWaitCount(c.Request.Context(), account.ID)
						accountWaitCounted = false
					}
				}

				accountReleaseFunc, err = h.concurrencyHelper.AcquireAccountSlotWithWaitTimeout(
					c,
					account.ID,
					selection.WaitPlan.MaxConcurrency,
					selection.WaitPlan.Timeout,
					reqStream,
					&streamStarted,
				)
				if err != nil {
					reqLog.Warn("gateway.account_slot_acquire_failed", zap.Int64("account_id", account.ID), zap.Error(err))
					releaseWait()
					h.handleConcurrencyError(c, err, "account", streamStarted)
					return
				}
				releaseWait()
				if err := h.gatewayService.BindStickySession(c.Request.Context(), apiKey.GroupID, sessionKey, account.ID); err != nil {
					reqLog.Warn("gateway.bind_sticky_session_failed", zap.Int64("account_id", account.ID), zap.Error(err))
				}
			}
			accountReleaseFunc = wrapReleaseOnDone(c.Request.Context(), accountReleaseFunc)

			// Forward to Copilot
			writerSizeBeforeForward := c.Writer.Size()
			result, err := h.gatewayService.ForwardCopilotAsMessages(c.Request.Context(), c, account, body, "")
			if accountReleaseFunc != nil {
				accountReleaseFunc()
			}
			if err != nil {
				var failoverErr *service.UpstreamFailoverError
				if errors.As(err, &failoverErr) {
					if c.Writer.Size() != writerSizeBeforeForward {
						h.handleFailoverExhausted(c, failoverErr, service.PlatformCopilot, true)
						return
					}
					action := fs.HandleFailoverError(c.Request.Context(), h.gatewayService, account.ID, account.Platform, failoverErr)
					switch action {
					case FailoverContinue:
						continue
					case FailoverExhausted:
						h.handleFailoverExhausted(c, fs.LastFailoverErr, service.PlatformCopilot, streamStarted)
						return
					case FailoverCanceled:
						return
					}
				}
				wroteFallback := h.ensureForwardErrorResponse(c, streamStarted)
				reqLog.Error("gateway.forward_failed",
					zap.Int64("account_id", account.ID),
					zap.String("account_platform", account.Platform),
					zap.Bool("fallback_error_response_written", wroteFallback),
					zap.Error(err),
				)
				return
			}

			// Record usage
			userAgent := c.GetHeader("User-Agent")
			clientIP := ip.GetClientIP(c)
			requestPayloadHash := service.HashUsageRequestPayload(body)
			inboundEndpoint := GetInboundEndpoint(c)
			upstreamEndpoint := GetUpstreamEndpoint(c, account.Platform)

			h.submitUsageRecordTask(func(ctx context.Context) {
				if err := h.gatewayService.RecordUsage(ctx, &service.RecordUsageInput{
					Result:             result,
					APIKey:             apiKey,
					User:               apiKey.User,
					Account:            account,
					Subscription:       subscription,
					InboundEndpoint:    inboundEndpoint,
					UpstreamEndpoint:   upstreamEndpoint,
					UserAgent:          userAgent,
					IPAddress:          clientIP,
					RequestPayloadHash: requestPayloadHash,
					ForceCacheBilling:  fs.ForceCacheBilling,
					APIKeyService:      h.apiKeyService,
				}); err != nil {
					logger.L().With(
						zap.String("component", "handler.gateway.messages"),
						zap.Int64("user_id", subject.UserID),
						zap.Int64("api_key_id", apiKey.ID),
						zap.Any("group_id", apiKey.GroupID),
						zap.String("model", reqModel),
						zap.Int64("account_id", account.ID),
					).Error("gateway.record_usage_failed", zap.Error(err))
				}
			})
			return
		}
	}
```

- [ ] **Step 2: Verify compilation**

Run:
```bash
cd backend && go build ./internal/handler/
```
Expected: no errors

- [ ] **Step 3: Commit**

```bash
git add backend/internal/handler/gateway_handler.go
git commit -m "feat(copilot): add Copilot branch to GatewayHandler.Messages

Copilot Messages requests are now handled by the main GatewayHandler
with its own failover loop, using GatewayService.ForwardCopilotAsMessages."
```

---

### Task 3: Update routing to send Copilot to main GatewayHandler

Change `/v1/messages` routing so Copilot groups go to `GatewayHandler.Messages` instead of `OpenAIGatewayHandler.Messages`.

**Files:**
- Modify: `backend/internal/server/routes/gateway.go:50-54`

- [ ] **Step 1: Update /v1/messages route**

In `backend/internal/server/routes/gateway.go`, change the `/v1/messages` route handler from:

```go
		gateway.POST("/messages", func(c *gin.Context) {
			platform := getGroupPlatform(c)
			if platform == service.PlatformOpenAI || platform == service.PlatformCopilot {
				h.OpenAIGateway.Messages(c)
				return
			}
			h.Gateway.Messages(c)
		})
```

To:

```go
		gateway.POST("/messages", func(c *gin.Context) {
			platform := getGroupPlatform(c)
			if platform == service.PlatformCopilot {
				h.Gateway.Messages(c)
				return
			}
			if platform == service.PlatformOpenAI {
				h.OpenAIGateway.Messages(c)
				return
			}
			h.Gateway.Messages(c)
		})
```

- [ ] **Step 2: Verify compilation**

Run:
```bash
cd backend && go build ./internal/server/...
```
Expected: no errors

- [ ] **Step 3: Commit**

```bash
git add backend/internal/server/routes/gateway.go
git commit -m "feat(copilot): route Copilot /v1/messages to main GatewayHandler

Copilot groups now use GatewayHandler instead of OpenAIGatewayHandler
for /v1/messages, completing the routing decoupling."
```

---

### Task 4: Revert OpenAI Gateway Copilot hacks

Remove all PlatformCopilot checks from OpenAI Gateway files, restoring them to OpenAI-only behavior.

**Files:**
- Modify: `backend/internal/service/openai_account_scheduler.go:587`
- Modify: `backend/internal/service/openai_gateway_service.go:1526-1542,1583,1605`
- Modify: `backend/internal/handler/openai_gateway_handler.go:510`
- Modify: `backend/internal/service/openai_gateway_messages.go:35-38`

- [ ] **Step 1: Revert openai_account_scheduler.go**

In `backend/internal/service/openai_account_scheduler.go` line 587, change:

```go
		if !account.IsSchedulable() || (!account.IsOpenAI() && account.Platform != PlatformCopilot) {
```

To:

```go
		if !account.IsSchedulable() || !account.IsOpenAI() {
```

- [ ] **Step 2: Revert openai_gateway_service.go — resolveFreshSchedulableOpenAIAccount**

In `backend/internal/service/openai_gateway_service.go` line 1583, change:

```go
	if !fresh.IsSchedulable() || (!fresh.IsOpenAI() && fresh.Platform != PlatformCopilot) {
```

To:

```go
	if !fresh.IsSchedulable() || !fresh.IsOpenAI() {
```

- [ ] **Step 3: Revert openai_gateway_service.go — recheckSelectedOpenAIAccountFromDB**

In `backend/internal/service/openai_gateway_service.go` line 1605, change:

```go
	if !latest.IsSchedulable() || (!latest.IsOpenAI() && latest.Platform != PlatformCopilot) {
```

To:

```go
	if !latest.IsSchedulable() || !latest.IsOpenAI() {
```

- [ ] **Step 4: Revert openai_gateway_service.go — listSchedulableAccounts**

In `backend/internal/service/openai_gateway_service.go`, replace the entire `listSchedulableAccounts` method (lines 1526-1559) from:

```go
func (s *OpenAIGatewayService) listSchedulableAccounts(ctx context.Context, groupID *int64, platform string) ([]Account, error) {
	if platform == "" {
		// 自动检测：先尝试 Copilot，如果有就用 Copilot，否则用 OpenAI
		if groupID != nil {
			var copilotAccounts []Account
			var err error
			if s.schedulerSnapshot != nil {
				copilotAccounts, _, err = s.schedulerSnapshot.ListSchedulableAccounts(ctx, groupID, PlatformCopilot, false)
			} else {
				copilotAccounts, err = s.accountRepo.ListSchedulableByGroupIDAndPlatform(ctx, *groupID, PlatformCopilot)
			}
			if err == nil && len(copilotAccounts) > 0 {
				return copilotAccounts, nil
			}
		}
		platform = PlatformOpenAI
	}
	if s.schedulerSnapshot != nil {
		accounts, _, err := s.schedulerSnapshot.ListSchedulableAccounts(ctx, groupID, platform, false)
		return accounts, err
	}
```

To:

```go
func (s *OpenAIGatewayService) listSchedulableAccounts(ctx context.Context, groupID *int64) ([]Account, error) {
	if s.schedulerSnapshot != nil {
		accounts, _, err := s.schedulerSnapshot.ListSchedulableAccounts(ctx, groupID, PlatformOpenAI, false)
		return accounts, err
	}
```

- [ ] **Step 5: Update callers of listSchedulableAccounts**

In `backend/internal/service/openai_gateway_service.go`, update the two callers:

Line ~1135, change:
```go
	accounts, err := s.listSchedulableAccounts(ctx, groupID, "")
```
To:
```go
	accounts, err := s.listSchedulableAccounts(ctx, groupID)
```

Line ~1336, change:
```go
	accounts, err := s.listSchedulableAccounts(ctx, groupID, "")
```
To:
```go
	accounts, err := s.listSchedulableAccounts(ctx, groupID)
```

- [ ] **Step 6: Revert openai_gateway_handler.go — AllowMessagesDispatch**

In `backend/internal/handler/openai_gateway_handler.go` line ~510, change:

```go
	if apiKey.Group != nil && apiKey.Group.Platform != service.PlatformCopilot && !apiKey.Group.AllowMessagesDispatch {
```

To:

```go
	if apiKey.Group != nil && !apiKey.Group.AllowMessagesDispatch {
```

- [ ] **Step 7: Revert openai_gateway_messages.go — ForwardAsAnthropic**

In `backend/internal/service/openai_gateway_messages.go`, remove the Copilot check at lines ~35-38:

Remove these lines:
```go
	// Copilot 平台使用 Chat Completions API，需要特殊处理
	if account.Platform == PlatformCopilot {
		return s.forwardCopilotAsAnthropic(ctx, c, account, body, defaultMappedModel)
	}
```

- [ ] **Step 8: Verify compilation**

Run:
```bash
cd backend && go build ./...
```
Expected: no errors. The `forwardCopilotAsAnthropic` method still exists in `openai_copilot_messages.go` but is now unreferenced. This is expected — we'll delete that file in the next task.

- [ ] **Step 9: Commit**

```bash
git add backend/internal/service/openai_account_scheduler.go \
       backend/internal/service/openai_gateway_service.go \
       backend/internal/handler/openai_gateway_handler.go \
       backend/internal/service/openai_gateway_messages.go
git commit -m "refactor(copilot): revert all Copilot hacks from OpenAI Gateway

Remove PlatformCopilot checks from openai_account_scheduler,
openai_gateway_service, openai_gateway_handler, and openai_gateway_messages.
OpenAI Gateway is now pure OpenAI-only again."
```

---

### Task 5: Delete openai_copilot_messages.go

The logic has been migrated to `copilot_gateway_messages.go`. The old file is now unreferenced.

**Files:**
- Delete: `backend/internal/service/openai_copilot_messages.go`

- [ ] **Step 1: Delete the file**

```bash
rm backend/internal/service/openai_copilot_messages.go
```

- [ ] **Step 2: Verify compilation**

Run:
```bash
cd backend && go build ./...
```
Expected: clean build with no errors

- [ ] **Step 3: Commit**

```bash
git add backend/internal/service/openai_copilot_messages.go
git commit -m "refactor(copilot): delete openai_copilot_messages.go

Logic migrated to copilot_gateway_messages.go on GatewayService."
```

---

### Task 6: Full build verification and smoke test

Verify the full build with the embed tag and run a basic compilation check.

**Files:**
- No changes — verification only

- [ ] **Step 1: Full build with embed tag**

Run:
```bash
cd backend && GOOS=linux GOARCH=amd64 CGO_ENABLED=0 GOPROXY=https://goproxy.cn,direct go build -tags embed -o /dev/null ./cmd/server
```
Expected: clean build, no errors

- [ ] **Step 2: Check for unused imports or variables**

Run:
```bash
cd backend && go vet ./...
```
Expected: no issues

- [ ] **Step 3: Verify no leftover PlatformCopilot references in OpenAI Gateway files**

Run:
```bash
grep -rn "PlatformCopilot" backend/internal/service/openai_*.go backend/internal/handler/openai_*.go
```
Expected: no output (zero matches)

- [ ] **Step 4: Verify Copilot logic is consolidated in copilot_* files**

Run:
```bash
grep -rn "PlatformCopilot\|ForwardCopilot\|copilotAPI" backend/internal/service/copilot_*.go
```
Expected: all Copilot references are in copilot_* files

- [ ] **Step 5: Commit verification notes (if any fixes were needed)**

If any fixes were needed, commit them with an appropriate message.
