package service

import (
	"context"
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

func TestOpenAIPassthroughForkInstructionsAreCallerOwned(t *testing.T) {
	for _, tc := range []struct {
		name, field string
	}{
		{"absent", ""},
		{"blank", `,"instructions":" "`},
		{"wrong type", `,"instructions":{"x":1}`},
		{"present", `,"instructions":"client rule"`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
			c.Request.Header.Set("User-Agent", "OpenAI/Python 2.8.0")
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			up := &httpUpstreamRecorder{resp: &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": []string{"text/event-stream"}}, Body: io.NopCloser(strings.NewReader("data: {\"type\":\"response.completed\",\"response\":{\"id\":\"r\",\"status\":\"completed\",\"output\":[],\"usage\":{\"input_tokens\":3,\"output_tokens\":1}}}\n\ndata: [DONE]\n\n"))}}
			svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: up}
			a := &Account{ID: 11, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Credentials: map[string]any{"access_token": "local-synthetic"}, Extra: map[string]any{"openai_passthrough": true}, Status: StatusActive, Schedulable: true, RateMultiplier: f64p(1)}
			_, err := svc.Forward(context.Background(), c, a, []byte(`{"model":"gpt-5.3-codex","stream":true,"input":[{"role":"user","content":"hi"}]`+tc.field+`}`))
			if tc.name == "wrong type" {
				require.Error(t, err)
				require.Nil(t, up.lastReq)
				require.Equal(t, http.StatusBadRequest, rec.Code)
				require.Equal(t, "instructions", gjson.GetBytes(rec.Body.Bytes(), "error.param").String())
				return
			}
			require.NoError(t, err)
			require.Equal(t, http.StatusOK, rec.Code)
			require.NotNil(t, up.lastReq)
			require.False(t, gjson.GetBytes(up.lastBody, "instructions").Exists(), "codex exec 0.171.0 omits top-level instructions")
			input := gjson.GetBytes(up.lastBody, "input").Array()
			if tc.name == "present" {
				require.Len(t, input, 2)
				require.Equal(t, "developer", input[0].Get("role").String())
				require.Equal(t, "client rule", input[0].Get("content.0.text").String())
				require.Equal(t, "model.base_instructions", input[0].Get("internal_chat_message_metadata_passthrough.content_item_kinds.0").String())
				require.Equal(t, "user", input[1].Get("role").String())
			} else {
				require.Len(t, input, 1, "absent or blank instructions must not inject server instructions")
				require.Equal(t, "user", input[0].Get("role").String())
			}
		})
	}
}
