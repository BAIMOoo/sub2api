//go:build codexlive

package integration_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/Wei-Shaw/sub2api/internal/repository"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
)

type codexLiveConfig struct {
	CredentialType string `json:"credential_type"`
	AccessToken    string `json:"access_token"`
	ChatGPTAccount string `json:"chatgpt_account_id"`
	Model          string `json:"model"`
}

func TestCodexHTTPPinnedLive(t *testing.T) {
	path := os.Getenv("SUB2API_CODEX_LIVE_CONFIG")
	if path == "" {
		t.Skip("no approved test credential supplied; live evidence is unverified")
	}
	live := readCodexLiveConfig(t, path)
	account := service.Account{
		ID: 1, Platform: service.PlatformOpenAI, Type: service.AccountTypeOAuth, Concurrency: 1,
		Credentials: map[string]any{"access_token": live.AccessToken, "chatgpt_account_id": live.ChatGPTAccount},
	}
	switch live.CredentialType {
	case "oauth":
	case "pat":
		account.Credentials["auth_mode"] = service.OpenAIAuthModePersonalAccessToken
	case "setup-token":
		account.Type = service.AccountTypeSetupToken
	default:
		t.Fatal("invalid credential type in private live config")
	}

	// Provider errors can contain reflected request details. Keep test output
	// limited to the route, declared credential type and result classification.
	logger.InitBootstrap()
	level := logger.CurrentLevel()
	if err := logger.SetLevel("fatal"); err != nil {
		t.Fatal("could not configure private live logging")
	}
	t.Cleanup(func() { _ = logger.SetLevel(level) })
	ginMode := gin.Mode()
	gin.SetMode(gin.TestMode)
	t.Cleanup(func() { gin.SetMode(ginMode) })

	cfg := &config.Config{}
	upstream := repository.NewHTTPUpstream(cfg)
	gateway := service.NewOpenAIGatewayService(nil, nil, nil, nil, nil, nil, nil, cfg,
		nil, nil, nil, nil, nil, upstream, nil, nil, nil, nil, nil, nil, nil, nil)

	for _, route := range []struct {
		name, path, shapeKey, shape string
	}{
		{"responses", "/v1/responses", "object", "response"},
		{"passthrough", "/v1/responses", "object", "response"},
		{"chat-completions", "/v1/chat/completions", "object", "chat.completion"},
		{"messages", "/v1/messages", "type", "message"},
		{"compact", "/v1/responses/compact", "object", "response.compaction"},
	} {
		t.Run(route.name, func(t *testing.T) {
			body := codexLiveBody(t, live.Model, route.name)
			attemptAccount := account
			attemptAccount.Extra = map[string]any{"openai_passthrough": route.name == "passthrough"}
			receipt := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(receipt)
			ctx, cancel := context.WithTimeout(t.Context(), 60*time.Second)
			defer cancel()
			c.Request = httptest.NewRequestWithContext(ctx, http.MethodPost, route.path, bytes.NewReader(body))
			c.Request.Header.Set("Content-Type", "application/json")
			c.Request.Header.Set("User-Agent", "sub2api-candidate-live/1")
			service.SetOpenAIClientTransport(c, service.OpenAIClientTransportHTTP)

			var result *service.OpenAIForwardResult
			var err error
			switch route.name {
			case "chat-completions":
				result, err = gateway.ForwardAsChatCompletions(ctx, c, &attemptAccount, body, "", "")
			case "messages":
				result, err = gateway.ForwardAsAnthropic(ctx, c, &attemptAccount, body, "", "")
			default:
				result, err = gateway.Forward(ctx, c, &attemptAccount, body)
			}
			media, _, _ := mime.ParseMediaType(receipt.Header().Get("Content-Type"))
			var reply map[string]json.RawMessage
			jsonOK := json.Unmarshal(receipt.Body.Bytes(), &reply) == nil
			var shape, status string
			shapeOK := json.Unmarshal(reply[route.shapeKey], &shape) == nil && shape == route.shape
			if route.shape == "response" {
				shapeOK = shapeOK && json.Unmarshal(reply["status"], &status) == nil && status == "completed"
			}
			complete := err == nil && result != nil && receipt.Code == http.StatusOK && media == "application/json" && jsonOK && shapeOK
			t.Logf("credential_type=%s route=%s status=%d valid_json=%t expected_shape=%t completed=%t",
				live.CredentialType, route.name, receipt.Code, jsonOK, shapeOK, complete)
			if !complete {
				t.Fatal("live forwarding did not complete; response and provider error withheld")
			}
		})
	}
}

func readCodexLiveConfig(t *testing.T, path string) codexLiveConfig {
	t.Helper()
	file, err := os.Open(path)
	if err != nil {
		t.Fatal("private live config unavailable")
	}
	defer func() { _ = file.Close() }()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0o077 != 0 || info.Size() > 64<<10 {
		t.Fatal("live config must be a private regular file (at most 64 KiB)")
	}
	decoder := json.NewDecoder(io.LimitReader(file, 64<<10))
	decoder.DisallowUnknownFields()
	var live codexLiveConfig
	if err := decoder.Decode(&live); err != nil ||
		strings.TrimSpace(live.Model) == "" || strings.TrimSpace(live.AccessToken) == "" ||
		strings.TrimSpace(live.ChatGPTAccount) == "" || !errors.Is(decoder.Decode(new(json.RawMessage)), io.EOF) {
		t.Fatal("invalid private live config")
	}
	return live
}

func codexLiveBody(t *testing.T, model, route string) []byte {
	t.Helper()
	user := []map[string]string{{"role": "user", "content": "Reply with exactly OK."}}
	request := map[string]any{"model": model, "stream": false}
	switch route {
	case "chat-completions", "messages":
		request["messages"] = user
		if route == "messages" {
			request["max_tokens"] = 64
		}
	case "compact":
		request["instructions"] = "Preserve the facts from this conversation."
		request["input"] = []map[string]string{{"role": "user", "content": "The synthetic project name is cedar and its port is 8123."}}
	default:
		request["input"] = user
	}
	body, err := json.Marshal(request)
	if err != nil {
		t.Fatal("could not encode synthetic live request")
	}
	return body
}
