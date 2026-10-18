package codexprobe

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type memoryConn struct{ bytes.Buffer }

func (c *memoryConn) Close() error                     { return nil }
func (c *memoryConn) LocalAddr() net.Addr              { return nil }
func (c *memoryConn) RemoteAddr() net.Addr             { return nil }
func (c *memoryConn) SetDeadline(time.Time) error      { return nil }
func (c *memoryConn) SetReadDeadline(time.Time) error  { return nil }
func (c *memoryConn) SetWriteDeadline(time.Time) error { return nil }

func testProbe(t *testing.T, limit int) (context.Context, Config) {
	t.Helper()
	digest := sha256.Sum256([]byte("socks5h://user:password@proxy:1080"))
	cfg := Config{Secret: strings.Repeat("s", 40), APIKeyID: 31, AccountID: 41,
		ProxyID: 51, ProxySHA256: hex.EncodeToString(digest[:]), OutputDir: t.TempDir(), MaxRequests: limit}
	ticket := Ticket{Run: fmt.Sprintf("run-%d", time.Now().UnixNano()), Sample: "native-standard", Expires: time.Now().Add(time.Hour).Unix()}
	raw, _ := json.Marshal(ticket)
	encoded := base64.RawURLEncoding.EncodeToString(raw)
	mac := hmac.New(sha256.New, []byte(cfg.Secret))
	_, _ = mac.Write([]byte(encoded))
	req := httptest.NewRequest("POST", "https://gateway/v1/responses", nil)
	req.Header.Set(Header, encoded+"."+hex.EncodeToString(mac.Sum(nil)))
	auth, err := authenticate(req, 31, cfg, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if auth.Header.Get(Header) != "" {
		t.Fatal("probe authentication leaked upstream")
	}
	if _, err := authenticate(req, 32, cfg, time.Now()); err == nil {
		t.Fatal("different API key accepted")
	}
	if _, err := authenticate(req, 31, cfg, time.Now().Add(2*time.Hour)); err == nil {
		t.Fatal("expired ticket accepted")
	}
	req.Header.Set(Header, req.Header.Get(Header)+"0")
	if _, err := authenticate(req, 31, cfg, time.Now()); err == nil {
		t.Fatal("forged signature accepted")
	}
	return auth.Context(), cfg
}

func TestProbeFragmentedWireRedactionAndRestartBudget(t *testing.T) {
	ctx, cfg := testProbe(t, 1)
	body := `{"input":"private prompt","prompt_cache_key":"session","client_metadata":{"session_id":"session","thread_id":"thread","x-codex-window-id":"window"},"unknown_field":"must remain visible"}`
	head := fmt.Sprintf("POST /backend-api/codex/responses HTTP/1.1\r\nversion: 0.158.0\r\nauthorization: Bearer NEVER_SAVE_ME\r\ncookie: secret-cookie\r\nsession-id: session\r\nthread-id: thread\r\nx-codex-window-id: window\r\nhost: chatgpt.com\r\ncontent-type: application/json\r\ncontent-length: %d\r\n\r\n", len(body))
	dst := &memoryConn{}
	conn := WrapHTTP(ctx, dst)
	wire := head + body
	for start := 0; start < len(wire); start += 7 {
		end := start + 7
		if end > len(wire) {
			end = len(wire)
		}
		if n, err := conn.Write([]byte(wire[start:end])); err != nil || n != end-start {
			t.Fatalf("fragment: %d %v", n, err)
		}
	}
	_ = conn.Close()
	if dst.String() != wire {
		t.Fatal("observer changed the wire")
	}
	files, _ := filepath.Glob(filepath.Join(cfg.OutputDir, "*", "*-http-*.json"))
	if len(files) != 1 {
		t.Fatalf("evidence files: %v", files)
	}
	raw, _ := os.ReadFile(files[0])
	for _, secret := range []string{"NEVER_SAVE_ME", "secret-cookie", "private prompt", "must remain visible"} {
		if bytes.Contains(raw, []byte(secret)) {
			t.Fatalf("persisted %s", secret)
		}
	}
	if !bytes.Contains(raw, []byte("unknown_field")) {
		t.Fatal("silently dropped unknown field")
	}
	// Simulate process-local counters being lost on restart.
	p := FromContext(ctx)
	p.run.count = 0
	other := &memoryConn{}
	if _, err := WrapHTTP(ctx, other).Write([]byte(wire)); err == nil {
		t.Fatal("restart bypassed budget")
	}
	if other.Len() != 0 {
		t.Fatal("over-budget bytes reached upstream")
	}
}

func TestProbeSelectionProxyAndIncompleteEvidence(t *testing.T) {
	ctx, cfg := testProbe(t, 3)
	if CheckSelection(ctx, 42, 51, true) == nil {
		t.Fatal("wrong account accepted")
	}
	if CheckSelection(ctx, 41, 52, true) == nil {
		t.Fatal("proxy fallback accepted")
	}
	req := httptest.NewRequest("POST", "https://chatgpt.com/backend-api/codex/responses", nil)
	p := FromContext(ctx)
	if p.CheckTransport(req, 41, "") == nil {
		t.Fatal("direct fallback accepted")
	}
	if p.CheckTransport(req, 41, "socks5h://user:password@proxy:1080") != nil {
		t.Fatal("expected proxy refused")
	}
	dst := &memoryConn{}
	conn := WrapHTTP(ctx, dst)
	_, err := conn.Write([]byte("POST /backend-api/codex/responses HTTP/1.1\r\nhost: chatgpt.com\r\ncontent-length: 100\r\n\r\n{}"))
	if err != nil {
		t.Fatal(err)
	}
	_ = conn.Close()
	files, _ := filepath.Glob(filepath.Join(cfg.OutputDir, "*", "*-incomplete-*.json"))
	if len(files) != 1 {
		t.Fatal("partial capture did not produce incomplete evidence")
	}
}

func TestProbeRejectsUnboundedBodiesBeforeSending(t *testing.T) {
	for _, header := range []string{"transfer-encoding: chunked", "content-length: 99999999", "expect: 100-continue\r\ncontent-length: 1"} {
		ctx, _ := testProbe(t, 3)
		dst := &memoryConn{}
		_, err := WrapHTTP(ctx, dst).Write([]byte("POST /backend-api/codex/responses HTTP/1.1\r\nhost: chatgpt.com\r\n" + header + "\r\n\r\n"))
		if err == nil || dst.Len() != 0 {
			t.Fatalf("unbounded request sent: %s", header)
		}
	}
}

func TestProbeClientHelloFragmentation(t *testing.T) {
	ctx, cfg := testProbe(t, 3)
	// Minimal ClientHello with supported_versions, no random material retained.
	body := []byte{3, 3}
	body = append(body, bytes.Repeat([]byte{0x77}, 32)...)
	body = append(body, 0, 0, 2, 0x13, 1, 1, 0, 0, 7, 0, 43, 0, 3, 2, 3, 4)
	hello := append([]byte{1, 0, 0, byte(len(body))}, body...)
	record := append([]byte{22, 3, 1, 0, byte(len(hello))}, hello...)
	dst := &memoryConn{}
	conn := WrapTLS(ctx, dst)
	for _, b := range record {
		if _, err := conn.Write([]byte{b}); err != nil {
			t.Fatal(err)
		}
	}
	if !bytes.Equal(dst.Bytes(), record) {
		t.Fatal("TLS observer changed bytes")
	}
	files, _ := filepath.Glob(filepath.Join(cfg.OutputDir, "*", "*-tls-*.json"))
	if len(files) != 1 {
		t.Fatal("missing TLS evidence")
	}
	raw, _ := os.ReadFile(files[0])
	if bytes.Contains(raw, []byte(strings.Repeat("w", 32))) {
		t.Fatal("TLS random persisted")
	}
	if !bytes.Contains(raw, []byte("772")) {
		t.Fatal("TLS supported version missing")
	}
}
