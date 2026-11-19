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
			require.NoError(t, err)
			require.Equal(t, http.StatusOK, rec.Code)
			require.NotNil(t, up.lastReq)
			field := gjson.GetBytes(up.lastBody, "instructions")
			if tc.field == "" {
				require.False(t, field.Exists())
			} else {
				require.JSONEq(t,
					gjson.Get(`{`+strings.TrimPrefix(tc.field, ",")+`}`, "instructions").Raw,
					field.Raw,
				)
			}
		})
	}
}
