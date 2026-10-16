package service

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// Exercise system-message preservation and upstream search-history repair
// through both transform branches. Codex exec 0.158.0 declares standard tools
// at the top level and Lite tools in input additional_tools.
func TestForward_CodexForkIdentityWithUpstreamSearchHistory(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, lite := range []bool{false, true} {
		for _, bridge := range []bool{false, true} {
			for _, functionTools := range []bool{false, true} {
				t.Run(fmt.Sprintf("lite=%v/bridge=%v/functions=%v", lite, bridge, functionTools), func(t *testing.T) {
					const systemPrompt = "Preserve this system guidance in input."
					request := map[string]any{
						"model": "gpt-5.6-sol", "stream": false,
						"input": []any{
							map[string]any{"type": "message", "role": "system", "content": systemPrompt},
							map[string]any{"type": "message", "role": "user", "content": "summarize"},
							map[string]any{"type": "web_search_call", "id": "ws_history", "status": "completed", "action": map[string]any{"type": "search", "query": "history"}},
							map[string]any{"type": "compaction_trigger"},
						},
						"tools": []any{}, "tool_choice": "auto",
					}
					if bridge {
						request["prompt_cache_key"] = "anthropic-cache-merge-regression"
					}
					if functionTools {
						request["tools"] = []any{map[string]any{
							"type": "function", "name": "lookup", "description": "Look up a value.",
							"parameters": map[string]any{"type": "object"}, "strict": true,
						}}
						request["tool_choice"] = map[string]any{"type": "function", "name": "lookup"}
					}
					body, err := json.Marshal(request)
					require.NoError(t, err)
					c, _ := gin.CreateTestContext(httptest.NewRecorder())
					c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
					c.Request.Header.Set("Content-Type", "application/json")
					if lite {
						c.Request.Header.Set(responsesLiteHeader, "true")
					}
					upstream := &httpUpstreamRecorder{resp: &http.Response{
						StatusCode: http.StatusOK,
						Header:     http.Header{"Content-Type": []string{"text/event-stream"}},
						Body:       io.NopCloser(strings.NewReader("data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_merge\",\"output\":[],\"usage\":{\"input_tokens\":1,\"output_tokens\":1}}}\n\ndata: [DONE]\n\n")),
					}}
					svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
					account := codexBodyEncodingTestAccount(530)
					account.Concurrency = 1
					account.Status = StatusActive
					account.Schedulable = true

					result, err := svc.Forward(context.Background(), c, account, body)
					require.NoError(t, err)
					require.NotNil(t, result)
					require.NotNil(t, upstream.lastReq)
					require.Equal(t, codexRequestBodyContentEncoding, upstream.lastReq.Header.Get("Content-Encoding"))
					forwarded := upstream.lastBody // recorder decodes the zstd wire body
					require.True(t, gjson.ValidBytes(forwarded))
					require.False(t, gjson.GetBytes(forwarded, "instructions").Exists(), "system guidance must stay in input without synthesized instructions")
					require.Equal(t, 1, strings.Count(string(forwarded), systemPrompt))
					message := gjson.GetBytes(forwarded, `input.#(content=="`+systemPrompt+`")`)
					require.Equal(t, "developer", message.Get("role").String())
					require.True(t, gjson.GetBytes(forwarded, `input.#(type=="web_search_call")`).Exists())
					items := gjson.GetBytes(forwarded, "input").Array()
					require.Equal(t, "compaction_trigger", items[len(items)-1].Get("type").String())

					searchPath := `tools.#(type=="web_search")`
					if lite {
						searchPath = `input.#(type=="additional_tools").tools.#(type=="web_search")`
						topLevelTools := gjson.GetBytes(forwarded, "tools")
						if topLevelTools.Exists() {
							require.True(t, topLevelTools.IsArray())
						}
						require.Empty(t, topLevelTools.Array(), "Lite may preserve an empty tools array")
						require.False(t, gjsonToolsContainWebSearch(topLevelTools), "Lite hosted search belongs in additional_tools")
					}
					search := gjson.GetBytes(forwarded, searchPath)
					require.Equal(t, "web_search", search.Get("type").String())
					require.Equal(t, gjson.False, search.Get("external_web_access").Type)
					if functionTools {
						require.Equal(t, "lookup", gjson.GetBytes(forwarded, "tool_choice.name").String())
						toolPath := `tools.#(type=="function")`
						if lite {
							toolPath = `input.#(type=="additional_tools").tools.#(type=="namespace").tools.0`
						} else {
							require.False(t, gjson.GetBytes(forwarded, `input.#(type=="additional_tools")`).Exists())
						}
						tool := gjson.GetBytes(forwarded, toolPath)
						require.Equal(t, "lookup", tool.Get("name").String())
						require.Equal(t, "Look up a value.", tool.Get("description").String())
						require.True(t, tool.Get("strict").Bool())
						require.Equal(t, "object", tool.Get("parameters.type").String())
					} else {
						require.Equal(t, "none", gjson.GetBytes(forwarded, "tool_choice").String())
					}
				})
			}
		}
	}
}

func TestApplyCodexOAuthTransform_CompactKeepsForkSystemGuidanceWithoutSearchInjection(t *testing.T) {
	request := map[string]any{
		"model": "gpt-5.6-sol", "instructions": "Caller instructions.",
		"input": []any{
			map[string]any{"type": "message", "role": "system", "content": "System guidance."},
			map[string]any{"type": "web_search_call", "status": "completed"},
		},
		"tools": []any{},
	}
	result := applyCodexOAuthTransformWithOptions(request, codexOAuthTransformOptions{
		IsCompact: true, ResponsesLite: true, SkipDefaultInstructions: true, KeepSystemMessagesInInput: true,
	})
	require.NoError(t, result.Error)
	require.Equal(t, "Caller instructions.", request["instructions"])
	input, ok := request["input"].([]any)
	require.True(t, ok)
	message, ok := input[0].(map[string]any)
	require.True(t, ok)
	require.Equal(t, "developer", message["role"])
	require.Equal(t, "System guidance.", message["content"])
	require.Empty(t, request["tools"])
	require.False(t, codexInputAdditionalToolsContainType(input, "web_search"))
}
