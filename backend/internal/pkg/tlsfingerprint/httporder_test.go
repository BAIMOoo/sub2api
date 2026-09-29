//go:build unit

package tlsfingerprint

import (
	"bytes"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"testing"
)

// 靶子：官方 codex 客户端 HTTP/1.1 POST /backend-api/codex/responses 的实测头顺序。
// 两次采集（2026-09-28 的 gpt-5.1-codex、2026-09-29 的 gpt-6-astra，见
// httporder.go 的注释），各版本各 2 条、四次一致。
var (
	// lite 模型（官方内置目录里 use_responses_lite=true）且 cookie jar 为空。
	officialOrderWithLite = []string{
		"version", "x-codex-beta-features", "x-codex-window-id", "x-codex-turn-metadata",
		"x-openai-internal-codex-responses-lite", "x-codex-routing-hint", "x-client-request-id",
		"session-id", "thread-id", "accept", "content-encoding", "content-type", "authorization",
		"chatgpt-account-id", "originator", "user-agent", "host", "content-length",
	}
	// lite 模型 + 带 cookie（cookie 位置固定在 user-agent 与 host 之间）。
	officialOrderWithLiteAndCookie = []string{
		"version", "x-codex-beta-features", "x-codex-window-id", "x-codex-turn-metadata",
		"x-openai-internal-codex-responses-lite", "x-codex-routing-hint", "x-client-request-id",
		"session-id", "thread-id", "accept", "content-encoding", "content-type", "authorization",
		"chatgpt-account-id", "originator", "user-agent", "cookie", "host", "content-length",
	}
	// 非 lite 模型（2026-09-28 的 gpt-5.1-codex）：不发 lite 头，其余位置不变。
	officialOrderWithoutLite = []string{
		"version", "x-codex-beta-features", "x-codex-window-id", "x-codex-turn-metadata",
		"x-codex-routing-hint", "x-client-request-id", "session-id", "thread-id", "accept",
		"content-encoding", "content-type", "authorization", "chatgpt-account-id", "originator",
		"user-agent", "host", "content-length",
	}
)

const officialContentLength = `{"model":"gpt-6-astra"}`

// newOfficialRequest 造一个「贴近官方出站」的请求：头集合与线上实测一致，
// 但刻意按 Go 的默认写头方式（Host 最前、其余按 key 字典序）交给 wrapper 重排。
func newOfficialRequest(t *testing.T, rawURL string, withLite, withCookie bool) *http.Request {
	t.Helper()
	body := []byte(officialContentLength)
	req, err := http.NewRequest("POST", rawURL, bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	u, err := url.Parse(rawURL)
	if err != nil {
		t.Fatal(err)
	}
	req.Host = u.Host
	req.ContentLength = int64(len(body))
	req.Header.Set("Version", "0.158.0")
	req.Header.Set("X-Codex-Beta-Features", "remote_compaction_v2")
	req.Header.Set("X-Codex-Window-Id", "01a0eb38-2f1b-7942-9c2d-ec2581ac1ed5:0")
	req.Header.Set("X-Codex-Turn-Metadata", `{"turn_id":"x"}`)
	req.Header.Set("X-Codex-Routing-Hint", "model=gpt-6-astra")
	req.Header.Set("X-Client-Request-Id", "01a0eb38-2f1b-7942-9c2d-ec2581ac1ed5")
	req.Header.Set("Session-Id", "01a0eb38-2f1b-7942-9c2d-ec2581ac1ed5")
	req.Header.Set("Thread-Id", "01a0eb38-2f1b-7942-9c2d-ec2581ac1ed5")
	req.Header.Set("Accept", "text/event-stream")
	req.Header.Set("Content-Encoding", "zstd")
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer <redacted>")
	req.Header.Set("Chatgpt-Account-Id", "acct")
	req.Header.Set("Originator", "codex_exec")
	req.Header.Set("User-Agent", "codex_exec/0.158.0 (Ubuntu 24.4.0; x86_64) xterm-256color (codex_exec; 0.158.0)")
	if withLite {
		req.Header.Set("X-OpenAI-Internal-Codex-Responses-Lite", "true")
	}
	if withCookie {
		req.Header.Set("Cookie", "__cf_bm=x")
	}
	return req
}

// headerNames 取「请求行之后、空行之前」的头名（小写，按线上出现顺序）。
func headerNames(raw []byte) []string {
	end := bytes.Index(raw, []byte("\r\n\r\n"))
	if end < 0 {
		return nil
	}
	lines := bytes.Split(raw[:end], []byte("\r\n"))
	out := make([]string, 0, len(lines)-1)
	for _, line := range lines[1:] {
		if i := bytes.IndexByte(line, ':'); i > 0 {
			out = append(out, strings.ToLower(string(line[:i])))
		}
	}
	return out
}

// writeThroughConn 把 raw 交给 wrapper 写出，返回对端实际收到的字节。
func writeThroughConn(t *testing.T, raw []byte) []byte {
	t.Helper()
	client, server := net.Pipe()
	defer func() { _ = client.Close() }()
	defer func() { _ = server.Close() }()
	got := make(chan []byte, 1)
	go func() {
		b, _ := io.ReadAll(server)
		got <- b
	}()
	wrapped := NewOrderedWriteConn(client, OpenAIResponsesHeaderOrder())
	if _, err := wrapped.Write(raw); err != nil {
		t.Fatalf("wrapper write: %v", err)
	}
	_ = wrapped.Close()
	return <-got
}

func assertOrder(t *testing.T, raw []byte, want []string) {
	t.Helper()
	got := headerNames(raw)
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("头顺序不符\nwant %v\n got %v", want, got)
	}
}

// TestOpenAIHeaderOrderMatchesOfficialTarget 覆盖靶子的三种真实形态（有无 lite 头、有无 cookie）。
func TestOpenAIHeaderOrderMatchesOfficialTarget(t *testing.T) {
	for _, tc := range []struct {
		name       string
		withLite   bool
		withCookie bool
		want       []string
	}{
		{"lite+无cookie", true, false, officialOrderWithLite},
		{"lite+cookie", true, true, officialOrderWithLiteAndCookie},
		{"非lite模型", false, false, officialOrderWithoutLite},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := newOfficialRequest(t, "https://chatgpt.com/backend-api/codex/responses", tc.withLite, tc.withCookie)
			var raw bytes.Buffer
			if err := req.Write(&raw); err != nil {
				t.Fatal(err)
			}
			// 前提：Go 默认把 Host 写在最前，说明本用例确实在测重排。
			if first := headerNames(raw.Bytes())[0]; first != "host" {
				t.Fatalf("前提不成立：Go 应把 host 写在最前，实际 %q", first)
			}
			out := writeThroughConn(t, raw.Bytes())
			assertOrder(t, out, tc.want)
			if !bytes.Contains(out, []byte(officialContentLength)) {
				t.Fatal("正文没有完整送达")
			}
		})
	}
}

// TestOpenAIHeaderOrderKeepsRequestLineAndLowercasesNames 校验只重排头行：
// 请求行逐字节不变、头名全小写、content-length 的值与实际正文一致。
func TestOpenAIHeaderOrderKeepsRequestLineAndLowercasesNames(t *testing.T) {
	req := newOfficialRequest(t, "https://chatgpt.com/backend-api/codex/responses", true, true)
	var raw bytes.Buffer
	if err := req.Write(&raw); err != nil {
		t.Fatal(err)
	}
	out := writeThroughConn(t, raw.Bytes())

	wantLine := strings.SplitN(raw.String(), "\r\n", 2)[0]
	gotLine := strings.SplitN(string(out), "\r\n", 2)[0]
	if gotLine != wantLine {
		t.Fatalf("请求行被改动\nwant %q\n got %q", wantLine, gotLine)
	}
	for _, name := range headerNames(out) {
		if name != strings.ToLower(name) {
			t.Fatalf("头名未小写：%s", name)
		}
	}
	if !bytes.Contains(out, []byte(fmt.Sprintf("content-length: %d\r\n", len(officialContentLength)))) {
		t.Fatalf("content-length 与实际正文长度不一致:\n%s", out)
	}
}

// TestOpenAIHeaderOrderUnknownHeaderFallsBackBeforeHost 表外的头排在 host 之前，
// 且多个表外头之间保持原有相对顺序。
func TestOpenAIHeaderOrderUnknownHeaderFallsBackBeforeHost(t *testing.T) {
	req := newOfficialRequest(t, "https://chatgpt.com/backend-api/codex/responses", true, false)
	req.Header.Set("X-Gateway-Trace-Id", "trace-1")
	req.Header.Set("X-Gateway-Session-Kind", "user")
	var raw bytes.Buffer
	if err := req.Write(&raw); err != nil {
		t.Fatal(err)
	}
	out := writeThroughConn(t, raw.Bytes())

	got := headerNames(out)
	want := append([]string{}, officialOrderWithLite[:len(officialOrderWithLite)-2]...)
	// Go 默认按 key 字典序写头，两个表外头的相对顺序因此是 session-kind 在前。
	want = append(want, "x-gateway-session-kind", "x-gateway-trace-id", "host", "content-length")
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("表外头落位不符\nwant %v\n got %v", want, got)
	}
}

// TestOpenAIHeaderOrderNonMatchingRequestUntouched 非目标 host / 非目标路径必须逐字节透传。
func TestOpenAIHeaderOrderNonMatchingRequestUntouched(t *testing.T) {
	for _, target := range []string{
		"https://chatgpt.com/backend-api/codex/models",
		"https://api.example.com/backend-api/codex/responses",
	} {
		t.Run(target, func(t *testing.T) {
			req, err := http.NewRequest("GET", target, nil)
			if err != nil {
				t.Fatal(err)
			}
			req.Host = mustHost(t, target)
			req.Header.Set("Authorization", "Bearer <redacted>")
			var raw bytes.Buffer
			if err := req.Write(&raw); err != nil {
				t.Fatal(err)
			}
			if out := writeThroughConn(t, raw.Bytes()); !bytes.Equal(out, raw.Bytes()) {
				t.Fatalf("非目标请求被改写\nwant %q\n got %q", raw.Bytes(), out)
			}
		})
	}
}

// TestOpenAIHeaderOrderHeaderBlockSplitAcrossWrites 覆盖真实场景：头块很大
// （x-codex-turn-metadata）或 bufio 分两次刷出时，头块会跨多次 Write 到达。
func TestOpenAIHeaderOrderHeaderBlockSplitAcrossWrites(t *testing.T) {
	req := newOfficialRequest(t, "https://chatgpt.com/backend-api/codex/responses", true, true)
	req.Header.Set("X-Codex-Turn-Metadata", `{"pad":"`+strings.Repeat("x", 5000)+`"}`)
	var raw bytes.Buffer
	if err := req.Write(&raw); err != nil {
		t.Fatal(err)
	}
	full := raw.Bytes()
	cut := bytes.Index(full, []byte("\r\n\r\n")) / 2

	client, server := net.Pipe()
	defer func() { _ = client.Close() }()
	defer func() { _ = server.Close() }()
	got := make(chan []byte, 1)
	go func() {
		b, _ := io.ReadAll(server)
		got <- b
	}()
	wrapped := NewOrderedWriteConn(client, OpenAIResponsesHeaderOrder())
	if _, err := wrapped.Write(full[:cut]); err != nil {
		t.Fatal(err)
	}
	if _, err := wrapped.Write(full[cut:]); err != nil {
		t.Fatal(err)
	}
	_ = wrapped.Close()

	out := <-got
	assertOrder(t, out, officialOrderWithLiteAndCookie)
	if !bytes.Contains(out, []byte(strings.Repeat("x", 5000))) {
		t.Fatal("大头（turn-metadata）的值没有完整保留")
	}
}

// TestNewOrderedWriteConnWithoutRules 没有可用规则时就该原样返回，不引入包装层。
func TestNewOrderedWriteConnWithoutRules(t *testing.T) {
	client, server := net.Pipe()
	defer func() { _ = client.Close() }()
	defer func() { _ = server.Close() }()
	for _, rules := range [][]*HTTPHeaderOrder{nil, {nil}, {&HTTPHeaderOrder{Host: "chatgpt.com"}}} {
		if got := NewOrderedWriteConn(client, rules...); got != net.Conn(client) {
			t.Fatalf("空规则不应包装连接，rules=%v", rules)
		}
	}
}

func mustHost(t *testing.T, rawURL string) string {
	t.Helper()
	u, err := url.Parse(rawURL)
	if err != nil {
		t.Fatal(err)
	}
	return u.Host
}
