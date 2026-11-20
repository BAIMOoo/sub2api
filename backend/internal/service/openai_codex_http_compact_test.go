package service

import (
	"bytes"
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

const codexHTTPCompactInput = `{
	"model":"gpt-5.5","instructions":"caller task","stream":true,
	"input":[{"type":"message","id":"msg_original","role":"user","content":"caller history"},
		{"type":"reasoning","id":"rs_original","encrypted_content":"opaque-in","summary":[]}],
	"reasoning":{"effort":"high"},
	"prompt_cache_key":"caller-session",
	"client_metadata":{"x-codex-turn-metadata":"{\"session_id\":\"caller-session\",\"caller\":9007199254740993}"},
	"tools":[{"type":"function","name":"read_file","parameters":{"type":"object"}}],
	"parallel_tool_calls":false,"tool_choice":"none"
}`

const codexHTTPCompactOutputItem = `{"type":"compaction","id":"cmp_original","encrypted_content":"opaque-out","opaque_data":{"id":9007199254740993}}`
const codexHTTPCompactSSEItem = `data: {"type":"response.output_item.done","item":` + codexHTTPCompactOutputItem + "}\n\n"
const codexHTTPCompactSSECompleted = `data: {"type":"response.completed","response":{"id":"resp_original","object":"response","status":"completed","output":[],"usage":{"input_tokens":11,"output_tokens":2}}}` + "\n\n"

func TestCodexHTTPCompactAdaptsToRemoteCompactionV2(t *testing.T) {
	for _, passthrough := range []bool{false, true} {
		for _, lite := range []bool{false, true} {
			t.Run("passthrough="+boolString(passthrough)+"/lite="+boolString(lite), func(t *testing.T) {
				account := codexBodyEncodingTestAccount(4510)
				account.Extra = map[string]any{"openai_passthrough": passthrough}
				setCodexHTTPTestMode(account, "gpt-5.5", lite)
				result, err, recorder, upstream, c := forwardCodexHTTPCompactForTest(t, "/v1/responses/compact", account, codexHTTPCompactInput, codexHTTPCompactSSEItem+codexHTTPCompactSSECompleted)
				require.NoError(t, err)
				require.Equal(t, http.StatusOK, recorder.Code)
				require.Contains(t, recorder.Header().Get("Content-Type"), "application/json")
				require.Equal(t, "response.compaction", gjson.Get(recorder.Body.String(), "object").String())
				require.Equal(t, "resp_original", result.ResponseID)
				require.Equal(t, 11, result.Usage.InputTokens)
				require.False(t, result.Stream)
				require.Positive(t, gjson.Get(recorder.Body.String(), "created_at").Int())
				require.Equal(t, "msg_original", gjson.Get(recorder.Body.String(), "output.0.id").String())
				require.Equal(t, "input_text", gjson.Get(recorder.Body.String(), "output.0.content.0.type").String())
				require.Equal(t, "caller history", gjson.Get(recorder.Body.String(), "output.0.content.0.text").String())
				require.JSONEq(t, codexHTTPCompactOutputItem, gjson.Get(recorder.Body.String(), "output.1").Raw)
				require.Equal(t, "9007199254740993", gjson.Get(recorder.Body.String(), "output.1.opaque_data.id").Raw)

				require.Len(t, upstream.requests, 1)
				require.Equal(t, chatgptCodexURL, upstream.lastReq.URL.String())
				require.Equal(t, "/v1/responses/compact", c.Request.URL.Path)
				require.Equal(t, "text/event-stream", upstream.lastReq.Header.Get("Accept"))
				require.Contains(t, upstream.lastReq.Header.Get("x-codex-beta-features"), "remote_compaction_v2")
				require.True(t, gjson.GetBytes(upstream.lastBody, "stream").Bool())
				input := gjson.GetBytes(upstream.lastBody, "input").Array()
				require.Equal(t, "compaction_trigger", input[len(input)-1].Get("type").String())
				require.Contains(t, string(upstream.lastBody), "caller task")
				require.Contains(t, string(upstream.lastBody), "caller history")
				require.Contains(t, string(upstream.lastBody), "read_file")
				require.Equal(t, "opaque-in", gjson.GetBytes(upstream.lastBody, `input.#(id=="rs_original").encrypted_content`).String())
				require.Equal(t, "high", gjson.GetBytes(upstream.lastBody, "reasoning.effort").String())
				require.Equal(t, "none", gjson.GetBytes(upstream.lastBody, "tool_choice").String())
				metadata := upstream.lastReq.Header.Get("x-codex-turn-metadata")
				require.Equal(t, "compaction", gjson.Get(metadata, "request_kind").String())
				require.Equal(t, "9007199254740993", gjson.Get(metadata, "caller").Raw)
				require.Equal(t, gjson.GetBytes(upstream.lastBody, "prompt_cache_key").String(), gjson.Get(metadata, "session_id").String())
			})
		}
	}
}

func TestCodexHTTPCompactRejectsIncompleteUpstream(t *testing.T) {
	for _, passthrough := range []bool{false, true} {
		for _, tc := range []struct{ name, reply string }{
			{"missing completion", codexHTTPCompactSSEItem},
			{"legacy done", codexHTTPCompactSSEItem + strings.Replace(codexHTTPCompactSSECompleted, "response.completed", "response.done", 1)},
			{"missing compaction", codexHTTPCompactSSECompleted},
			{"item after completion", codexHTTPCompactSSECompleted + codexHTTPCompactSSEItem},
			{"malformed completion", codexHTTPCompactSSEItem + "event: response.completed\ndata: {\"response\":\n\n"},
			{"malformed upstream event", "data: {broken}\n\n" + codexHTTPCompactSSEItem + codexHTTPCompactSSECompleted},
			{"unfinished item", strings.Replace(codexHTTPCompactSSEItem, "output_item.done", "output_item.added", 1) + codexHTTPCompactSSECompleted},
			{"ambiguous compaction output", codexHTTPCompactSSEItem + strings.Replace(codexHTTPCompactSSEItem, "cmp_original", "cmp_second", 1) + codexHTTPCompactSSECompleted},
			{"terminal hides ambiguous output", codexHTTPCompactSSEItem + strings.Replace(codexHTTPCompactSSEItem, "cmp_original", "cmp_second", 1) + strings.Replace(codexHTTPCompactSSECompleted, `"output":[]`, `"output":[`+codexHTTPCompactOutputItem+`]`, 1)},
			{"incomplete", codexHTTPCompactSSEItem + `data: {"type":"response.incomplete","response":{"id":"resp_incomplete","output":[]}}` + "\n\n"},
			{"unary upstream", `{"id":"resp_original","object":"response.compaction","output":[` + codexHTTPCompactOutputItem + "]}"},
		} {
			t.Run(tc.name+"/passthrough="+boolString(passthrough), func(t *testing.T) {
				account := codexBodyEncodingTestAccount(4511)
				account.Extra = map[string]any{"openai_passthrough": passthrough}
				result, err, recorder, upstream, _ := forwardCodexHTTPCompactForTest(t, "/v1/responses/compact", account, codexHTTPCompactInput, tc.reply)
				require.Error(t, err)
				require.Nil(t, result)
				require.Len(t, upstream.requests, 1)
				require.Equal(t, http.StatusBadGateway, recorder.Code)
				require.Equal(t, "upstream_error", gjson.Get(recorder.Body.String(), "error.type").String())
			})
		}
	}
}

func TestCodexHTTPCompactLeavesAPIKeyCompactOnLegacyWire(t *testing.T) {
	account := &Account{Platform: PlatformOpenAI, Type: AccountTypeAPIKey, Credentials: map[string]any{"api_key": "test-key"}}
	reply := `{"id":"cmp_original","object":"response.compaction","output":[],"usage":{"input_tokens":1,"output_tokens":1}}`
	_, err, recorder, upstream, _ := forwardCodexHTTPCompactForTest(t, "/v1/responses/compact", account, codexHTTPCompactInput, reply)
	require.NoError(t, err)
	require.Equal(t, http.StatusOK, recorder.Code)
	require.Equal(t, openaiPlatformAPIURL+"/compact", upstream.lastReq.URL.String())
	require.False(t, HasCompactionTriggerInInput(upstream.lastBody))
	require.False(t, gjson.GetBytes(upstream.lastBody, "stream").Exists())
	require.False(t, gjson.GetBytes(upstream.lastBody, "tool_choice").Exists())
	require.Equal(t, "application/json", upstream.lastReq.Header.Get("Accept"))
}

func forwardCodexHTTPCompactForTest(t *testing.T, path string, account *Account, input, reply string) (*OpenAIForwardResult, error, *httptest.ResponseRecorder, *httpUpstreamRecorder, *gin.Context) {
	t.Helper()
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	body := []byte(input)
	c.Request = httptest.NewRequest(http.MethodPost, path, bytes.NewReader(body))
	c.Request.Header.Set("Accept", "application/json")
	c.Request.Header.Set("x-codex-beta-features", "feature-from-client")
	SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
	upstream := &httpUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": {"application/json"}},
		Body:       io.NopCloser(strings.NewReader(reply)),
	}}
	svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
	result, err := svc.Forward(c.Request.Context(), c, account, body)
	require.Equal(t, input, string(body))
	return result, err, recorder, upstream, c
}
