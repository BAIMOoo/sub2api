package codexprobe

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"strings"

	"github.com/klauspost/compress/zstd"
)

type httpCapture struct {
	net.Conn
	probe    *Probe
	pending  []byte
	head     []byte
	body     []byte
	request  *http.Request
	left     int64
	reserved bool
	complete bool
	failed   bool
}

// WrapHTTP must be inside the header-ordering connection and outside TLS:
// net/http -> ordering -> this observer -> TLS. Only probe transports use it.
func WrapHTTP(ctx context.Context, conn net.Conn) net.Conn {
	p := FromContext(ctx)
	if p == nil {
		return conn
	}
	return &httpCapture{Conn: conn, probe: p}
}

func (c *httpCapture) Write(b []byte) (int, error) {
	if c.failed || c.complete {
		return 0, ErrProbe
	}
	total := len(b)
	first := false
	if !c.reserved {
		c.pending = append(c.pending, b...)
		end := bytes.Index(c.pending, []byte("\r\n\r\n"))
		if end < 0 {
			if len(c.pending) > 64<<10 {
				c.failed = true
				return 0, ErrProbe
			}
			return total, nil
		}
		if end > 64<<10 {
			c.failed = true
			return 0, ErrProbe
		}
		c.head = append([]byte(nil), c.pending[:end+4]...)
		req, err := http.ReadRequest(bufio.NewReader(bytes.NewReader(c.head)))
		if err != nil || len(req.TransferEncoding) != 0 || req.ContentLength > maxBody ||
			req.ContentLength < 0 || req.Host != "chatgpt.com" || req.Header.Get("Expect") != "" {
			c.failed = true
			return 0, ErrProbe
		}
		if err := c.probe.reserve(req.Method, req.URL.Path); err != nil {
			c.failed = true
			return 0, err
		}
		c.request, c.left, c.reserved = req, req.ContentLength, true
		b = c.pending
		c.pending = nil
		first = true
	}
	body := b
	if first {
		body = b[len(c.head):]
	}
	if int64(len(body)) > c.left {
		c.failed = true
		return 0, ErrProbe
	}
	n, err := c.Conn.Write(b)
	if n != len(b) || err != nil {
		c.failed = true
		if err == nil {
			err = io.ErrShortWrite
		}
		return 0, err
	}
	c.body = append(c.body, body...)
	c.left -= int64(len(body))
	if c.left == 0 {
		c.complete = true
		if err := c.capture(); err != nil {
			c.failed = true
			return 0, err
		}
	}
	return total, nil
}

func (c *httpCapture) capture() error {
	order := make([]string, 0)
	headers := make(map[string]any)
	for _, line := range bytes.Split(c.head, []byte("\r\n"))[1:] {
		name, value, ok := strings.Cut(string(line), ":")
		if !ok {
			continue
		}
		order = append(order, name)
		lower, value := strings.ToLower(name), strings.TrimSpace(value)
		switch lower {
		case "authorization", "cookie", "chatgpt-account-id", "proxy-authorization":
			headers[lower] = "<redacted>"
		case "content-encoding", "content-type", "content-length", "host", "version",
			"originator", "x-openai-internal-codex-responses-lite", "accept":
			headers[lower] = value
		default:
			headers[lower] = c.probe.opaque(value)
		}
	}
	raw := c.body
	if c.request.Header.Get("Content-Encoding") == "zstd" {
		decoder, err := zstd.NewReader(bytes.NewReader(raw), zstd.WithDecoderMaxMemory(16<<20))
		if err != nil {
			return ErrProbe
		}
		defer decoder.Close()
		raw, err = io.ReadAll(io.LimitReader(decoder, maxBody+1))
		if err != nil || len(raw) > maxBody {
			return ErrProbe
		}
	} else if c.request.Header.Get("Content-Encoding") != "" {
		return ErrProbe
	}
	var body any
	if len(raw) > 0 && json.Unmarshal(raw, &body) != nil {
		return ErrProbe
	}
	checks := map[string]bool{}
	formats := map[string]string{}
	for _, name := range []string{"session-id", "thread-id", "x-codex-window-id"} {
		if value := c.request.Header.Get(name); value != "" {
			formats[name] = identityFormat(value)
		}
	}
	if obj, ok := body.(map[string]any); ok {
		cache, cacheOK := obj["prompt_cache_key"].(string)
		checks["session_matches_cache"] = cacheOK && cache != "" && c.request.Header.Get("session-id") == cache
		if meta, ok := obj["client_metadata"].(map[string]any); ok {
			checks["metadata_session_matches"] = meta["session_id"] == cache && cache != ""
			checks["metadata_thread_matches"] = meta["thread_id"] == c.request.Header.Get("thread-id")
			checks["window_matches"] = meta["x-codex-window-id"] == c.request.Header.Get("x-codex-window-id")
		}
	}
	return c.probe.Emit("http", map[string]any{
		"method": c.request.Method, "path": c.request.URL.Path,
		"header_order": order, "headers": headers, "wire_body_bytes": len(c.body),
		"body": c.probe.sanitize(body), "identity_checks": checks, "identity_formats": formats,
		"evidence": "gateway_final_http_before_tls", "cold_connection": true,
	})
}

func (c *httpCapture) Close() error {
	if c.reserved && (!c.complete || c.failed) {
		_ = c.probe.Emit("incomplete", map[string]any{"reason": "http_write_or_capture_incomplete"})
	}
	return c.Conn.Close()
}
