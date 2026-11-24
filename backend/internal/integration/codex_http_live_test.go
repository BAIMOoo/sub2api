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
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/Wei-Shaw/sub2api/internal/pkg/proxyurl"
	"github.com/Wei-Shaw/sub2api/internal/repository"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

type codexLiveConfig struct {
	CredentialType string `json:"credential_type"`
	AccessToken    string `json:"access_token"`
	ChatGPTAccount string `json:"chatgpt_account_id"`
	Model          string `json:"model"`
	ProxyURL       string `json:"proxy_url,omitempty"`
}

func TestCodexLiveConfigAllowsBearerOnlySetupToken(t *testing.T) {
	path := filepath.Join(t.TempDir(), "live.json")
	const configJSON = `{"credential_type":"setup-token","access_token":"synthetic-test-token","model":"gpt-5.5"}`
	if err := os.WriteFile(path, []byte(configJSON), 0o600); err != nil {
		t.Fatal(err)
	}
	live := readCodexLiveConfig(t, path)
	if live.CredentialType != "setup-token" || live.ChatGPTAccount != "" || live.ProxyURL != "" {
		t.Fatal("expected a bearer-only SetupToken without proxy")
	}
}

func TestCodexLiveProxyConfigParsing(t *testing.T) {
	proxyID, proxyCfg, err := buildCodexLiveProxy("")
	if err != nil || proxyID != nil || proxyCfg != nil {
		t.Fatalf("empty proxy_url should select direct egress, got id=%v cfg=%v err=%v", proxyID, proxyCfg, err)
	}

	proxyID, proxyCfg, err = buildCodexLiveProxy("socks5://user:pass@proxy.example.test:1080")
	if err != nil || proxyID == nil || *proxyID != 1 || proxyCfg == nil {
		t.Fatalf("valid socks5 proxy_url failed: id=%v cfg=%v err=%v", proxyID, proxyCfg, err)
	}
	if proxyCfg.Protocol != "socks5h" || proxyCfg.Host != "proxy.example.test" || proxyCfg.Port != 1080 ||
		proxyCfg.Username != "user" || proxyCfg.Password != "pass" || proxyCfg.URL() != "socks5h://user:pass@proxy.example.test:1080" {
		t.Fatalf("unexpected parsed proxy config: protocol=%s host=%s port=%d", proxyCfg.Protocol, proxyCfg.Host, proxyCfg.Port)
	}

	for _, invalid := range []string{
		"https://proxy.example.test:443",
		"http://proxy.example.test",
		"http://proxy.example.test:70000",
		"socks5h://useronly@proxy.example.test:1080",
		"://broken",
	} {
		if _, _, err := buildCodexLiveProxy(invalid); err == nil {
			t.Fatalf("expected error for invalid proxy_url %q", invalid)
		}
	}
}

func TestCodexLiveToolContinuationExtraction(t *testing.T) {
	rawOutput := json.RawMessage(`[
		{"type":"reasoning","id":"rs_1","encrypted_content":"opaque"},
		{"type":"function_call","id":"fc_1","call_id":"call_1","name":"lookup_service_port","arguments":"{\"service\":\"cedar\"}"}
	]`)
	items, callIDs, hasReasoning, hasMessage, err := inspectCodexLiveOutputItems(rawOutput)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(items) != 2 || len(callIDs) != 1 || callIDs[0] != "call_1" || !hasReasoning || hasMessage {
		t.Fatalf("unexpected turn1 inspection: items=%d calls=%v reasoning=%t message=%t", len(items), callIDs, hasReasoning, hasMessage)
	}

	finalOutput := json.RawMessage(`[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"8123"}]}]`)
	_, callIDs, hasReasoning, hasMessage, err = inspectCodexLiveOutputItems(finalOutput)
	if err != nil {
		t.Fatalf("unexpected final error: %v", err)
	}
	if len(callIDs) != 0 || hasReasoning || !hasMessage {
		t.Fatalf("unexpected turn2 inspection: calls=%v reasoning=%t message=%t", callIDs, hasReasoning, hasMessage)
	}
}

func TestCodexHTTPPinnedLive(t *testing.T) {
	path := os.Getenv("SUB2API_CODEX_LIVE_CONFIG")
	if path == "" {
		t.Skip("no approved test credential supplied; live evidence is unverified")
	}
	live := readCodexLiveConfig(t, path)
	proxyID, proxyCfg, err := buildCodexLiveProxy(live.ProxyURL)
	if err != nil {
		t.Fatal(err.Error())
	}
	account := service.Account{
		ID: 1, Platform: service.PlatformOpenAI, Type: service.AccountTypeOAuth, Concurrency: 1,
		ProxyID: proxyID, Proxy: proxyCfg,
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
			payload := map[string]any{"model": live.Model, "stream": false}
			maps.Copy(payload, route.fields)
			attemptAccount := account
			attemptAccount.Extra = map[string]any{"openai_passthrough": route.passthrough}
			res := runCodexLiveTurn(t, upstream, &attemptAccount, route.path, route.shapeKey, route.shape, payload, route.forward)
			t.Logf("credential_type=%s route=%s status=%d wire_mode=%s valid_json=%t expected_shape=%t completed=%t",
				live.CredentialType, route.name, res.statusCode, res.wireMode, res.jsonOK, res.shapeOK, res.complete)
			if !res.complete {
				t.Fatal("live forwarding did not complete; response and provider error withheld")
			}
		})
	}

	t.Run("tool-continuation", func(t *testing.T) {
		sessionID := uuid.NewString()
		tools := []map[string]any{{
			"type":        "function",
			"name":        "lookup_service_port",
			"description": "Return the configured port number for a synthetic service name.",
			"parameters": map[string]any{
				"type": "object",
				"properties": map[string]any{
					"service": map[string]any{"type": "string"},
				},
				"required":             []string{"service"},
				"additionalProperties": false,
			},
		}}
		const instructions = "You must call lookup_service_port before answering any question about a service port."
		userMessage := map[string]any{
			"type":    "message",
			"role":    "user",
			"content": "What port is configured for service cedar? Call lookup_service_port first, then reply with only the number.",
		}
		turn1Payload := map[string]any{
			"model":            live.Model,
			"stream":           false,
			"prompt_cache_key": sessionID,
			"instructions":     instructions,
			"tools":            tools,
			"tool_choice":      "auto",
			"input":            []any{userMessage},
		}
		attemptAccount := account
		attemptAccount.Extra = map[string]any{"openai_passthrough": false}
		turn1 := runCodexLiveTurn(t, upstream, &attemptAccount, "/v1/responses", "object", "response", turn1Payload, gateway.Forward)
		outputItems, callIDs, replayedReasoning, _, parseErr := inspectCodexLiveOutputItems(turn1.reply["output"])
		if !turn1.complete || parseErr != nil || len(callIDs) == 0 {
			t.Logf("credential_type=%s route=tool-continuation turn1_status=%d turn2_status=0 wire_mode=%s tool_calls=%d replayed_reasoning=%t final_message=false completed=false",
				live.CredentialType, turn1.statusCode, turn1.wireMode, len(callIDs), replayedReasoning)
			t.Fatal("live tool-continuation turn 1 did not emit a tool call; response and provider error withheld")
		}

		turn2Input := make([]any, 0, 1+len(outputItems)+len(callIDs))
		turn2Input = append(turn2Input, userMessage)
		for _, item := range outputItems {
			turn2Input = append(turn2Input, item)
		}
		for _, callID := range callIDs {
			turn2Input = append(turn2Input, map[string]any{
				"type":    "function_call_output",
				"call_id": callID,
				"output":  `{"port":8123}`,
			})
		}
		turn2Payload := map[string]any{
			"model":            live.Model,
			"stream":           false,
			"prompt_cache_key": sessionID,
			"instructions":     instructions,
			"tools":            tools,
			"tool_choice":      "auto",
			"input":            turn2Input,
		}
		turn2 := runCodexLiveTurn(t, upstream, &attemptAccount, "/v1/responses", "object", "response", turn2Payload, gateway.Forward)
		_, _, _, hasFinalMessage, turn2ParseErr := inspectCodexLiveOutputItems(turn2.reply["output"])
		complete := turn2.complete && turn2ParseErr == nil && hasFinalMessage
		t.Logf("credential_type=%s route=tool-continuation turn1_status=%d turn2_status=%d wire_mode=%s tool_calls=%d replayed_reasoning=%t final_message=%t completed=%t",
			live.CredentialType, turn1.statusCode, turn2.statusCode, turn2.wireMode, len(callIDs), replayedReasoning, hasFinalMessage, complete)
		if !complete {
			t.Fatal("live tool-continuation turn 2 did not complete with a final message; response and provider error withheld")
		}
	})
}

type codexLiveTurnResult struct {
	statusCode int
	wireMode   string
	jsonOK     bool
	shapeOK    bool
	complete   bool
	reply      map[string]json.RawMessage
}

func runCodexLiveTurn(
	t *testing.T,
	upstream *codexLiveUpstream,
	account *service.Account,
	path, shapeKey, wantShape string,
	payload map[string]any,
	forward func(context.Context, *gin.Context, *service.Account, []byte) (*service.OpenAIForwardResult, error),
) codexLiveTurnResult {
	t.Helper()
	upstream.evidence = nil
	body, err := json.Marshal(payload)
	if err != nil {
		t.Fatal("could not encode synthetic live request")
	}
	receipt := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(receipt)
	ctx, cancel := context.WithTimeout(t.Context(), 60*time.Second)
	defer cancel()
	c.Request = httptest.NewRequestWithContext(ctx, http.MethodPost, path, bytes.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Request.Header.Set("User-Agent", "sub2api-candidate-live/1")
	service.SetOpenAIClientTransport(c, service.OpenAIClientTransportHTTP)

	result, err := forward(ctx, c, account, body)
	media, _, _ := mime.ParseMediaType(receipt.Header().Get("Content-Type"))
	var reply map[string]json.RawMessage
	jsonOK := json.Unmarshal(receipt.Body.Bytes(), &reply) == nil
	var shape, status string
	shapeOK := json.Unmarshal(reply[shapeKey], &shape) == nil && shape == wantShape
	if wantShape == "response" {
		shapeOK = shapeOK && json.Unmarshal(reply["status"], &status) == nil && status == "completed"
	}
	wireMode, upstreamCompleted := "unverified", false
	if evidence := upstream.evidence; evidence != nil {
		wireMode, upstreamCompleted = evidence.wireMode, evidence.completed()
	}
	complete := err == nil && result != nil && receipt.Code == http.StatusOK && media == "application/json" &&
		jsonOK && shapeOK && upstreamCompleted
	return codexLiveTurnResult{
		statusCode: receipt.Code,
		wireMode:   wireMode,
		jsonOK:     jsonOK,
		shapeOK:    shapeOK,
		complete:   complete,
		reply:      reply,
	}
}

func inspectCodexLiveOutputItems(raw json.RawMessage) ([]json.RawMessage, []string, bool, bool, error) {
	if len(bytes.TrimSpace(raw)) == 0 {
		return nil, nil, false, false, nil
	}
	var items []json.RawMessage
	if err := json.Unmarshal(raw, &items); err != nil {
		return nil, nil, false, false, err
	}
	var callIDs []string
	var hasReasoning, hasMessage bool
	for _, item := range items {
		var meta struct {
			Type             string `json:"type"`
			ID               string `json:"id"`
			CallID           string `json:"call_id"`
			EncryptedContent string `json:"encrypted_content"`
		}
		if err := json.Unmarshal(item, &meta); err != nil {
			return nil, nil, false, false, err
		}
		switch meta.Type {
		case "function_call":
			if id := strings.TrimSpace(meta.CallID); id != "" {
				callIDs = append(callIDs, id)
			}
		case "reasoning":
			if strings.TrimSpace(meta.ID) != "" && strings.TrimSpace(meta.EncryptedContent) != "" {
				hasReasoning = true
			}
		case "message":
			hasMessage = true
		}
	}
	return items, callIDs, hasReasoning, hasMessage, nil
}

func buildCodexLiveProxy(raw string) (*int64, *service.Proxy, error) {
	_, parsed, err := proxyurl.Parse(raw)
	if err != nil {
		return nil, nil, errors.New("invalid proxy_url in private live config")
	}
	if parsed == nil {
		return nil, nil, nil
	}
	if parsed.Scheme == "https" {
		return nil, nil, errors.New("https proxy_url is incompatible with fixed Codex TLS transport")
	}
	portText := parsed.Port()
	if portText == "" {
		return nil, nil, errors.New("proxy_url must include an explicit port")
	}
	port, err := strconv.Atoi(portText)
	if err != nil || port < 1 || port > 65535 {
		return nil, nil, errors.New("proxy_url port out of range")
	}
	var username, password string
	if parsed.User != nil {
		username = parsed.User.Username()
		password, _ = parsed.User.Password()
		if (username != "" && password == "") || (username == "" && password != "") {
			return nil, nil, errors.New("proxy_url authentication requires both username and password")
		}
	}
	proxyID := int64(1)
	return &proxyID, &service.Proxy{
		ID:       proxyID,
		Protocol: parsed.Scheme,
		Host:     parsed.Hostname(),
		Port:     port,
		Username: username,
		Password: password,
		Status:   service.StatusActive,
	}, nil
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
