package main

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func TestBridgeProbeIdentityAndSSE(t *testing.T) {
	h := probeHandler("source-0123456789abcdef")
	identity := httptest.NewRecorder()
	h.ServeHTTP(identity, httptest.NewRequest(http.MethodGet, "/identity", nil))
	if identity.Code != 200 || !strings.Contains(identity.Body.String(), "source-0123456789abcdef") {
		t.Fatal("identity is missing")
	}
	stream := httptest.NewRecorder()
	h.ServeHTTP(stream, httptest.NewRequest(http.MethodGet, "/sse?count=3&interval_ms=10", nil))
	if !stream.Flushed || strings.Count(stream.Body.String(), "event: probe\n") != 3 || !strings.Contains(stream.Body.String(), "event: probe_done\n") {
		t.Fatalf("stream is incomplete: %s", stream.Body.String())
	}
	invalid := httptest.NewRecorder()
	h.ServeHTTP(invalid, httptest.NewRequest(http.MethodGet, "/sse?count=100000&interval_ms=1000", nil))
	if invalid.Code != 400 {
		t.Fatal("unbounded stream accepted")
	}
}

func TestBridgeProbeStandardWebSocketEcho(t *testing.T) {
	server := httptest.NewServer(probeHandler("target-0123456789abcdef"))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	conn, response, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(server.URL, "http")+"/ws", nil)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.CloseNow()
	if response.Header.Get("X-Probe-Identity") != "target-0123456789abcdef" {
		t.Fatal("upgrade identity missing")
	}
	for _, kind := range []websocket.MessageType{websocket.MessageText, websocket.MessageBinary} {
		if err := conn.Write(ctx, kind, []byte("exact echo")); err != nil {
			t.Fatal(err)
		}
		gotKind, payload, err := conn.Read(ctx)
		if err != nil || gotKind != kind || string(payload) != "exact echo" {
			t.Fatalf("echo: kind=%v payload=%q error=%v", gotKind, payload, err)
		}
	}
	if err := conn.Close(websocket.StatusNormalClosure, "done"); err != nil && err != io.EOF {
		t.Fatal(err)
	}
}
