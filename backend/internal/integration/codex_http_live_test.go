//go:build codexlive

package integration_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/repository"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
)

// Explicitly opt-in: the private config is created by the operator on the test
// host and never belongs in the repository. Only safe status receipts are logged.
// This calls forwarding directly, without deploying an app or writing usage/DB.
func TestCodexHTTPPinnedLive(t *testing.T) {
	path := os.Getenv("SUB2API_CODEX_LIVE_CONFIG")
	if path == "" {
		t.Skip("set SUB2API_CODEX_LIVE_CONFIG to opt into serial live OAuth/PAT tests")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal("private live config unavailable")
	}
	var live struct {
		Account service.Account
		Model   string
	}
	if json.Unmarshal(data, &live) != nil || live.Model == "" || !live.Account.IsOpenAIOAuthLike() {
		t.Fatal("invalid private live config")
	}
	gin.SetMode(gin.TestMode)
	cfg := &config.Config{}
	upstream := repository.NewHTTPUpstream(cfg)
	svc := service.NewOpenAIGatewayService(nil, nil, nil, nil, nil, nil, nil, cfg,
		nil, nil, nil, nil, nil, upstream, nil, nil, nil, nil, nil, nil, nil, nil)
	for _, route := range []string{"responses", "passthrough", "chat/completions", "messages", "compact"} {
		t.Run(route, func(t *testing.T) {
			request := map[string]any{"model": live.Model, "stream": false}
			switch route {
			case "compact":
				request["instructions"] = "Preserve the facts from this conversation."
				request["input"] = []any{map[string]any{"role": "user", "content": "The synthetic project name is cedar. Its port is 8123. Preserve both facts."}}
			case "responses", "passthrough":
				request["instructions"] = "Reply with exactly OK."
				request["input"] = []any{map[string]any{"role": "user", "content": "Say OK."}}
			case "chat/completions":
				request["messages"] = []any{map[string]any{"role": "system", "content": "Reply with exactly OK."}, map[string]any{"role": "user", "content": "Say OK."}}
			case "messages":
				request["system"] = "Reply with exactly OK."
				request["max_tokens"] = 16
				request["messages"] = []any{map[string]any{"role": "user", "content": "Say OK."}}
			}
			body, err := json.Marshal(request)
			if err != nil {
				t.Fatal("encode synthetic request")
			}
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
			defer cancel()
			account := live.Account
			account.Extra = map[string]any{"openai_oauth_passthrough": route == "passthrough"}
			requestPath := "/v1/" + route
			if route == "passthrough" {
				requestPath = "/v1/responses"
			}
			if route == "compact" {
				requestPath = "/v1/responses/compact"
			}
			c.Request = httptest.NewRequest(http.MethodPost, requestPath, bytes.NewReader(body)).WithContext(ctx)
			c.Request.Header.Set("User-Agent", "synthetic-arbitrary-harness/1")
			service.SetOpenAIClientTransport(c, service.OpenAIClientTransportHTTP)
			var result *service.OpenAIForwardResult
			switch route {
			case "chat/completions":
				result, err = svc.ForwardAsChatCompletions(ctx, c, &account, body, "", "")
			case "messages":
				result, err = svc.ForwardAsAnthropic(ctx, c, &account, body, "", "")
			default:
				result, err = svc.Forward(ctx, c, &account, body)
			}
			resultStream := false
			if result != nil {
				resultStream = result.Stream
			}
			t.Logf("route=%s status=%d completed=%t response_bytes=%d json=%t sse=%t stream=%t content_type=%q", route, recorder.Code, err == nil && result != nil, recorder.Body.Len(), json.Valid(recorder.Body.Bytes()), bytes.Contains(recorder.Body.Bytes(), []byte("data: ")), resultStream, recorder.Header().Get("Content-Type"))
			if err != nil || result == nil || recorder.Code != http.StatusOK || !json.Valid(recorder.Body.Bytes()) || !strings.Contains(recorder.Header().Get("Content-Type"), "application/json") {
				t.Fatal("live forwarding did not complete; inspect private operator logs, never publish credentials")
			}
		})
	}
}
