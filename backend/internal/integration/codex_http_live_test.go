//go:build codexlive

package integration_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"maps"
	"mime"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
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

func TestCodexLiveConfigAllowsBearerOnlySetupToken(t *testing.T) {
	path := filepath.Join(t.TempDir(), "live.json")
	const configJSON = `{"credential_type":"setup-token","access_token":"synthetic-test-token","model":"gpt-5.5"}`
	if err := os.WriteFile(path, []byte(configJSON), 0o600); err != nil {
		t.Fatal(err)
	}
	live := readCodexLiveConfig(t, path)
	if live.CredentialType != "setup-token" || live.ChatGPTAccount != "" {
		t.Fatal("expected a bearer-only SetupToken")
	}
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
	upstream := &codexLiveUpstream{upstream: repository.NewHTTPUpstream(cfg)}
	gateway := service.NewOpenAIGatewayService(nil, nil, nil, nil, nil, nil, nil, cfg,
		nil, nil, nil, nil, nil, upstream, nil, nil, nil, nil, nil, nil, nil, nil)
	t.Cleanup(gateway.CloseOpenAIWSPool)
	chat := func(ctx context.Context, c *gin.Context, account *service.Account, body []byte) (*service.OpenAIForwardResult, error) {
		return gateway.ForwardAsChatCompletions(ctx, c, account, body, "", "")
	}
	messages := func(ctx context.Context, c *gin.Context, account *service.Account, body []byte) (*service.OpenAIForwardResult, error) {
		return gateway.ForwardAsAnthropic(ctx, c, account, body, "", "")
	}

	user := []map[string]string{{"role": "user", "content": "Reply with exactly OK."}}
	for _, route := range []struct {
		name, path, shapeKey, shape string
		fields                      map[string]any
		forward                     func(context.Context, *gin.Context, *service.Account, []byte) (*service.OpenAIForwardResult, error)
		passthrough                 bool
	}{
		{
			name: "responses", path: "/v1/responses", shapeKey: "object", shape: "response",
			fields: map[string]any{"input": user}, forward: gateway.Forward,
		},
		{
			name: "passthrough", path: "/v1/responses", shapeKey: "object", shape: "response",
			fields: map[string]any{"input": user}, forward: gateway.Forward, passthrough: true,
		},
		{
			name: "chat-completions", path: "/v1/chat/completions", shapeKey: "object", shape: "chat.completion",
			fields: map[string]any{"messages": user}, forward: chat,
		},
		{
			name: "messages", path: "/v1/messages", shapeKey: "type", shape: "message",
			fields: map[string]any{"messages": user, "max_tokens": 64}, forward: messages,
		},
		{
			name: "compact", path: "/v1/responses/compact", shapeKey: "object", shape: "response.compaction",
			fields: map[string]any{
				"instructions": "Preserve the facts from this conversation.",
				"input":        []map[string]string{{"role": "user", "content": "The synthetic project name is cedar and its port is 8123."}},
			},
			forward: gateway.Forward,
		},
	} {
		t.Run(route.name, func(t *testing.T) {
			upstream.evidence = nil
			payload := map[string]any{"model": live.Model, "stream": false}
			maps.Copy(payload, route.fields)
			body, err := json.Marshal(payload)
			if err != nil {
				t.Fatal("could not encode synthetic live request")
			}
			attemptAccount := account
			attemptAccount.Extra = map[string]any{"openai_passthrough": route.passthrough}
			receipt := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(receipt)
			ctx, cancel := context.WithTimeout(t.Context(), 60*time.Second)
			defer cancel()
			c.Request = httptest.NewRequestWithContext(ctx, http.MethodPost, route.path, bytes.NewReader(body))
			c.Request.Header.Set("Content-Type", "application/json")
			c.Request.Header.Set("User-Agent", "sub2api-candidate-live/1")
			service.SetOpenAIClientTransport(c, service.OpenAIClientTransportHTTP)

			result, err := route.forward(ctx, c, &attemptAccount, body)
			media, _, _ := mime.ParseMediaType(receipt.Header().Get("Content-Type"))
			var reply map[string]json.RawMessage
			jsonOK := json.Unmarshal(receipt.Body.Bytes(), &reply) == nil
			var shape, status string
			shapeOK := json.Unmarshal(reply[route.shapeKey], &shape) == nil && shape == route.shape
			if route.shape == "response" {
				shapeOK = shapeOK && json.Unmarshal(reply["status"], &status) == nil && status == "completed"
			}
			wireMode, upstreamCompleted := "unverified", false
			if evidence := upstream.evidence; evidence != nil {
				wireMode, upstreamCompleted = evidence.wireMode, evidence.completed()
			}
			complete := err == nil && result != nil && receipt.Code == http.StatusOK && media == "application/json" &&
				jsonOK && shapeOK && upstreamCompleted
			t.Logf("credential_type=%s route=%s status=%d wire_mode=%s valid_json=%t expected_shape=%t completed=%t",
				live.CredentialType, route.name, receipt.Code, wireMode, jsonOK, shapeOK, complete)
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
		!errors.Is(decoder.Decode(new(json.RawMessage)), io.EOF) {
		t.Fatal("invalid private live config")
	}
	return live
}
