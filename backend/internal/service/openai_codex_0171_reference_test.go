package service

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
	"github.com/klauspost/compress/zstd"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func TestCodex0171HTTPWireReference(t *testing.T) {
	raw, err := os.ReadFile("testdata/codex_exec_0171_linux_amd64/http.json")
	require.NoError(t, err)
	var ref struct {
		RequestLine             string            `json:"request_line"`
		Headers                 map[string]string `json:"headers"`
		StandardOrder           []string          `json:"standard_and_compact_order"`
		LiteOrder               []string          `json:"lite_order"`
		StandardWithCookieOrder []string          `json:"standard_with_cookie_order"`
		ZstdHeaderHex           string            `json:"zstd_header_hex"`
	}
	require.NoError(t, json.Unmarshal(raw, &ref))
	frameHeader, err := hex.DecodeString(ref.ZstdHeaderHex)
	require.NoError(t, err)
	require.NotEmpty(t, frameHeader)
	decoder, err := zstd.NewReader(nil)
	require.NoError(t, err)
	t.Cleanup(decoder.Close)

	for _, tc := range []struct {
		name, path          string
		lite, cookie, tools bool
	}{
		{"Standard", "/v1/responses", false, false, false},
		{"Standard With Cookie", "/v1/responses", false, true, false},
		{"Lite No Tools", "/v1/responses", true, false, false},
		{"Lite With Tools", "/v1/responses", true, false, true},
		{"Compact V2", "/v1/responses/compact", false, false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			compact := tc.path == "/v1/responses/compact"
			input := []byte(`{"model":"gpt-5.5","instructions":"caller task","input":"caller history","stream":false}`)
			if tc.tools {
				input = []byte(`{"model":"gpt-5.5","instructions":"caller task","input":"caller history","tools":[{"type":"function","name":"read_file","description":"Read file","parameters":{"type":"object"}}],"stream":false}`)
			}
			reply := codexHTTPCompactSSECompleted
			if compact {
				input = []byte(codexHTTPCompactInput)
				reply = codexHTTPCompactSSEItem + reply
			}
			account := codexBodyEncodingTestAccount(171)
			setCodexHTTPTestMode(account, "gpt-5.5", tc.lite)
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, tc.path, bytes.NewReader(input))
			c.Request.Header.Set("User-Agent", "noncodex-harness/1")
			SetOpenAIClientTransport(c, OpenAIClientTransportHTTP)
			upstream := &codexHTTPTransportRecorder{httpUpstreamRecorder: httpUpstreamRecorder{
				resp: &http.Response{
					StatusCode: http.StatusOK,
					Header:     http.Header{"Content-Type": {"text/event-stream"}},
					Body:       io.NopCloser(strings.NewReader(reply)),
				},
			}}
			svc := &OpenAIGatewayService{cfg: &config.Config{}, httpUpstream: upstream}
			_, err := svc.Forward(c.Request.Context(), c, account, input)
			require.NoError(t, err)
			require.Len(t, upstream.profiles, 1)
			require.Zero(t, upstream.plainCalls)
			profile := upstream.profiles[0]
			require.True(t, profile.DisableCompression)

			req := upstream.lastReq.Clone(t.Context())
			if tc.cookie {
				req.Header.Set("Cookie", "__cf_bm=fixture-cookie")
			}
			req.Body, err = req.GetBody()
			require.NoError(t, err)
			t.Cleanup(func() { _ = req.Body.Close() })
			var wire codexHTTPWireBuffer
			require.NoError(t, req.Write(tlsfingerprint.NewOrderedWriteConn(&wire, profile.HeaderOrders()...)))
			head, encoded, found := bytes.Cut(wire.buf.Bytes(), []byte("\r\n\r\n"))
			require.True(t, found)
			lines := strings.Split(string(head), "\r\n")
			require.Equal(t, ref.RequestLine, lines[0])
			headerNames := make([]string, 0, len(lines)-1)
			headers := make(map[string]string, len(lines)-1)
			for _, line := range lines[1:] {
				name, value, found := strings.Cut(line, ": ")
				require.True(t, found, line)
				headerNames = append(headerNames, name)
				headers[name] = value
			}
			wantOrder := ref.StandardOrder
			if tc.cookie {
				wantOrder = ref.StandardWithCookieOrder
			} else if tc.lite {
				wantOrder = ref.LiteOrder
				require.Equal(t, "true", headers[strings.ToLower(responsesLiteHeader)])
			}
			require.Equal(t, wantOrder, headerNames)
			for name, value := range ref.Headers {
				require.Equal(t, value, headers[name], name)
			}
			require.Equal(t, strconv.Itoa(len(encoded)), headers["content-length"])
			require.True(t, bytes.HasPrefix(encoded, frameHeader), "fixed zstd frame header changed")
			decoded, err := decoder.DecodeAll(encoded, nil)
			require.NoError(t, err)
			attemptBody, fixed := codexHTTPAttemptBody(upstream.lastReq)
			require.True(t, fixed)
			require.Equal(t, attemptBody, decoded)
			require.Contains(t, string(decoded), "caller task")
			require.Contains(t, string(decoded), "caller history")
			require.Equal(t, compact, HasCompactionTriggerInInput(decoded))
			require.False(t, gjson.GetBytes(decoded, "instructions").Exists(), "codex exec 0.171.0 never sends top-level instructions")
			if tc.lite {
				require.False(t, gjson.GetBytes(decoded, "tools").Exists(), "Lite mode never sends top-level tools")
				if tc.tools {
					require.Equal(t, "additional_tools", gjson.GetBytes(decoded, "input.0.type").String())
					require.Equal(t, "developer", gjson.GetBytes(decoded, "input.1.role").String())
					require.Equal(t, "caller task", gjson.GetBytes(decoded, "input.1.content.0.text").String())
					require.Equal(t, "model.base_instructions", gjson.GetBytes(decoded, "input.1.internal_chat_message_metadata_passthrough.content_item_kinds.0").String())
				} else {
					require.False(t, gjson.GetBytes(decoded, `input.#(type=="additional_tools")`).Exists(), "Lite mode without tools must omit empty additional_tools carrier")
					require.Equal(t, "developer", gjson.GetBytes(decoded, "input.0.role").String())
					require.Equal(t, "caller task", gjson.GetBytes(decoded, "input.0.content.0.text").String())
					require.Equal(t, "model.base_instructions", gjson.GetBytes(decoded, "input.0.internal_chat_message_metadata_passthrough.content_item_kinds.0").String())
				}
			} else {
				require.True(t, gjson.GetBytes(decoded, "tools").Exists(), "Standard mode retains top-level tools")
				require.False(t, gjson.GetBytes(decoded, `input.#(type=="additional_tools")`).Exists())
				require.Equal(t, "developer", gjson.GetBytes(decoded, "input.0.role").String())
				require.Equal(t, "caller task", gjson.GetBytes(decoded, "input.0.content.0.text").String())
				require.Equal(t, "model.base_instructions", gjson.GetBytes(decoded, "input.0.internal_chat_message_metadata_passthrough.content_item_kinds.0").String())
			}
		})
	}
}

type codexHTTPWireBuffer struct {
	net.Conn
	buf bytes.Buffer
}

func (c *codexHTTPWireBuffer) Write(p []byte) (int, error) {
	return c.buf.Write(p)
}
