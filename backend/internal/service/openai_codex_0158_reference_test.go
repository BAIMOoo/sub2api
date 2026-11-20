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

func TestCodex0158HTTPWireReference(t *testing.T) {
	raw, err := os.ReadFile("testdata/codex_exec_0158_linux_amd64/http.json")
	require.NoError(t, err)
	var ref struct {
		RequestLine   string            `json:"request_line"`
		Headers       map[string]string `json:"headers"`
		StandardOrder []string          `json:"standard_and_compact_order"`
		LiteOrder     []string          `json:"lite_order"`
		ZstdHeaderHex string            `json:"zstd_header_hex"`
	}
	require.NoError(t, json.Unmarshal(raw, &ref))
	frameHeader, err := hex.DecodeString(ref.ZstdHeaderHex)
	require.NoError(t, err)
	require.NotEmpty(t, frameHeader)
	decoder, err := zstd.NewReader(nil)
	require.NoError(t, err)
	t.Cleanup(decoder.Close)

	for _, tc := range []struct {
		name, path string
		lite       bool
	}{
		{"Standard", "/v1/responses", false},
		{"Lite", "/v1/responses", true},
		{"Compact V2", "/v1/responses/compact", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			compact := tc.path == "/v1/responses/compact"
			input := []byte(`{"model":"gpt-5.5","instructions":"caller task","input":"caller history","stream":false}`)
			reply := codexHTTPCompactSSECompleted
			if compact {
				input = []byte(codexHTTPCompactInput)
				reply = codexHTTPCompactSSEItem + reply
			}
			account := codexBodyEncodingTestAccount(158)
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
			if tc.lite {
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
			require.Equal(t, !tc.lite, gjson.GetBytes(decoded, "instructions").Exists())
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
