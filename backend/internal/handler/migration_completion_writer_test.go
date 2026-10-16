package handler

import (
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
)

type migrationCompletionTestSink struct {
	*httptest.ResponseRecorder
	flushes  int
	closed   chan bool
	writeErr error
	writeMax int
}

func (s *migrationCompletionTestSink) Flush() {
	s.flushes++
	s.ResponseRecorder.Flush()
}
func (s *migrationCompletionTestSink) CloseNotify() <-chan bool { return s.closed }
func (s *migrationCompletionTestSink) Write(p []byte) (int, error) {
	if s.writeErr != nil {
		n := min(len(p), s.writeMax)
		_, _ = s.ResponseRecorder.Write(p[:n])
		return n, s.writeErr
	}
	return s.ResponseRecorder.Write(p)
}

func newMigrationCompletionTestWriter(t *testing.T, protocol MigrationCompletionProtocol) (*MigrationCompletionWriter, *migrationCompletionTestSink) {
	t.Helper()
	sink := &migrationCompletionTestSink{ResponseRecorder: httptest.NewRecorder(), closed: make(chan bool)}
	c, _ := gin.CreateTestContext(sink)
	w, err := NewMigrationCompletionWriter(c.Writer, MigrationCompletionOptions{
		Protocol: protocol, MemoryBytes: 64, MaxFrameBytes: 4096,
		MaxHeldBytes: 32 * 1024, SpoolDirectory: t.TempDir(),
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = w.Close() })
	return w, sink
}

func migrationCompletionWrite(t *testing.T, w *MigrationCompletionWriter, p string) {
	t.Helper()
	n, err := w.WriteString(p)
	if err != nil || n != len(p) {
		t.Fatalf("write = %d, %v; want %d, nil", n, err, len(p))
	}
}

func TestMigrationCompletionNonstreamPreservesLogicalGinState(t *testing.T) {
	w, sink := newMigrationCompletionTestWriter(t, MigrationCompletionNonStreaming)
	if w.Written() || w.Size() != -1 || w.Status() != 200 {
		t.Fatal("initial Gin state changed")
	}
	w.Header().Set("X-Before", "committed")
	w.WriteHeader(http.StatusCreated)
	migrationCompletionWrite(t, w, "accepted response")
	w.Flush()
	w.WriteHeader(http.StatusBadGateway) // Gin ignores this after logical write.
	w.Header().Set("X-Before", "too late")
	if !w.Written() || w.Size() != len("accepted response") || w.Status() != 201 {
		t.Fatalf("logical response changed: status=%d size=%d", w.Status(), w.Size())
	}
	if w.DownstreamWritten() || w.DownstreamSize() != -1 || sink.Body.Len() != 0 || sink.Header().Get("X-Before") != "" {
		t.Fatal("successful nonstream body or headers escaped before persistence")
	}
	if err := w.ConfirmPersisted(); !errors.Is(err, ErrMigrationCompletionNotFinished) {
		t.Fatalf("premature confirmation: %v", err)
	}
	if err := w.Finish(); err != nil {
		t.Fatal(err)
	}
	if err := w.ConfirmPersisted(); err != nil {
		t.Fatal(err)
	}
	if sink.Code != 201 || sink.Body.String() != "accepted response" || sink.Header().Get("X-Before") != "committed" {
		t.Fatalf("released response differs: %d %q %v", sink.Code, sink.Body.String(), sink.Header())
	}
	if !w.PersistenceConfirmed() || w.DownstreamSize() != w.Size() {
		t.Fatal("confirmation or physical accounting missing")
	}
	if err := w.ConfirmPersisted(); err != nil || sink.Body.String() != "accepted response" {
		t.Fatal("confirmation must be idempotent")
	}
	if _, err := w.WriteString("late"); !errors.Is(err, ErrMigrationCompletionFinished) {
		t.Fatalf("write after Finish: %v", err)
	}
}

func TestMigrationCompletionSSECrossWriteCRLFAndMultiline(t *testing.T) {
	w, sink := newMigrationCompletionTestWriter(t, MigrationCompletionResponses)
	w.Header().Set("Content-Type", "text/event-stream")
	heartbeat := ": ping\r\n\r\n"
	migrationCompletionWrite(t, w, heartbeat)
	if w.StreamDataForwarded() || sink.Body.String() != heartbeat {
		t.Fatal("heartbeat counted as effective data or was not forwarded")
	}
	delta := "event: response.output_text.delta\r\ndata: {\"type\":\"response.output_text.delta\",\r\ndata: \"delta\":\"hello\"}\r\n\r\n"
	for i := range delta {
		migrationCompletionWrite(t, w, delta[i:i+1])
		if i < len(delta)-1 && sink.Body.String() != heartbeat {
			t.Fatal("partial SSE frame was emitted before classification")
		}
	}
	if !w.StreamDataForwarded() || sink.Body.String() != heartbeat+delta || sink.flushes != 2 {
		t.Fatalf("nonterminal frame was not immediately flushed: %q, flushes=%d", sink.Body.String(), sink.flushes)
	}
	terminal := "event: response.completed\r\ndata: {\"type\":\"response.completed\",\"response\":{\"usage\":{}}}\r\n\r\n"
	after := "data: [DONE]\r\n\r\n: after terminal\r\n\r\n"
	for i := range terminal {
		migrationCompletionWrite(t, w, terminal[i:i+1])
	}
	migrationCompletionWrite(t, w, after)
	w.Flush()
	if !w.TerminalObserved() || sink.Body.String() != heartbeat+delta {
		t.Fatal("terminal or subsequent content escaped")
	}
	if w.Size() != len(heartbeat+delta+terminal+after) || w.DownstreamSize() != len(heartbeat+delta) {
		t.Fatal("logical and physical byte counts were conflated")
	}
	if err := w.Finish(); err != nil {
		t.Fatal(err)
	}
	if err := w.ConfirmPersisted(); err != nil {
		t.Fatal(err)
	}
	if sink.Body.String() != heartbeat+delta+terminal+after {
		t.Fatal("released SSE bytes changed")
	}
}

func TestMigrationCompletionRecognizedTerminals(t *testing.T) {
	cases := []struct {
		name     string
		protocol MigrationCompletionProtocol
		terminal string
	}{
		{"responses-completed", MigrationCompletionResponses, "data: {\"type\":\"response.completed\"}\n\n"},
		{"responses-done", MigrationCompletionResponses, "event: response.done\ndata: {}\n\n"},
		{"done-sentinel", MigrationCompletionChat, "data: [DONE]\n\n"},
		{"messages-stop", MigrationCompletionMessages, "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"},
		{"messages-delta-stop", MigrationCompletionMessages, "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"}}\n\n"},
		{"chat-finish", MigrationCompletionChat, "data: {\"choices\":[{\"index\":0,\"finish_reason\":null},{\"index\":1,\"finish_reason\":\"stop\"}]}\n\n"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w, sink := newMigrationCompletionTestWriter(t, tc.protocol)
			migrationCompletionWrite(t, w, tc.terminal)
			if !w.TerminalObserved() || sink.Body.Len() != 0 {
				t.Fatal("terminal was not held")
			}
			if err := w.Finish(); err != nil {
				t.Fatal(err)
			}
			if err := w.ConfirmPersisted(); err != nil || sink.Body.String() != tc.terminal {
				t.Fatalf("confirmation = %v, body=%q", err, sink.Body.String())
			}
		})
	}
}

func TestMigrationCompletionNullReasonsRemainNonterminal(t *testing.T) {
	for _, tc := range []struct {
		protocol MigrationCompletionProtocol
		frame    string
	}{
		{MigrationCompletionMessages, "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":null}}\n\n"},
		{MigrationCompletionChat, "data: {\"choices\":[{\"finish_reason\":null}]}\n\n"},
	} {
		w, sink := newMigrationCompletionTestWriter(t, tc.protocol)
		migrationCompletionWrite(t, w, tc.frame)
		if w.TerminalObserved() || sink.Body.String() != tc.frame {
			t.Fatal("null reason counted as terminal")
		}
		if err := w.Finish(); !errors.Is(err, ErrMigrationCompletionNoTerminal) {
			t.Fatalf("missing terminal accepted: %v", err)
		}
		if err := w.ConfirmPersisted(); !errors.Is(err, ErrMigrationCompletionNoTerminal) {
			t.Fatalf("missing terminal confirmed: %v", err)
		}
	}
}

func TestMigrationCompletionErrorsPassThrough(t *testing.T) {
	for _, protocol := range []MigrationCompletionProtocol{MigrationCompletionNonStreaming, MigrationCompletionResponses} {
		for _, code := range []int{400, 401, 429, 500, 503} {
			w, sink := newMigrationCompletionTestWriter(t, protocol)
			w.Header().Set("Retry-After", "2")
			w.WriteHeader(code)
			migrationCompletionWrite(t, w, "not SSE: upstream error")
			w.Flush()
			if sink.Code != code || sink.Body.String() != "not SSE: upstream error" || sink.Header().Get("Retry-After") != "2" {
				t.Fatalf("error response changed: %d %q", sink.Code, sink.Body.String())
			}
			if err := w.Finish(); err != nil {
				t.Fatal(err)
			}
			if err := w.Close(); err != nil {
				t.Fatal(err)
			}
		}
	}
}

func TestMigrationCompletionSpoolsAndCleansPrivateFiles(t *testing.T) {
	w, sink := newMigrationCompletionTestWriter(t, MigrationCompletionNonStreaming)
	body := strings.Repeat("x", 4096)
	migrationCompletionWrite(t, w, body)
	if w.held.file == nil || len(w.held.memory) != 0 {
		t.Fatal("large held body was not spooled")
	}
	path := w.held.file.Name()
	info, err := os.Stat(path)
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatalf("private spool mode: info=%v error=%v", info, err)
	}
	if sink.Body.Len() != 0 {
		t.Fatal("spooling leaked response")
	}
	if err := w.Finish(); err != nil {
		t.Fatal(err)
	}
	if err := w.ConfirmPersisted(); err != nil || sink.Body.String() != body {
		t.Fatalf("spool replay: %v", err)
	}
	if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("spool file retained: %v", err)
	}
}

func TestMigrationCompletionLimitsFailClosed(t *testing.T) {
	t.Run("held-body", func(t *testing.T) {
		w, sink := newMigrationCompletionTestWriter(t, MigrationCompletionNonStreaming)
		w.held.limit = 100
		n, err := w.Write([]byte(strings.Repeat("x", 10000)))
		if n != 100 || !errors.Is(err, ErrMigrationCompletionHoldLimit) || w.held.size != 100 {
			t.Fatalf("limit: n=%d error=%v size=%d", n, err, w.held.size)
		}
		if cap(w.held.memory) > w.options.MemoryBytes || w.held.file == nil {
			t.Fatal("held memory limit bypassed")
		}
		if err := w.Finish(); !errors.Is(err, ErrMigrationCompletionHoldLimit) {
			t.Fatalf("Finish ignored limit failure: %v", err)
		}
		if err := w.ConfirmPersisted(); !errors.Is(err, ErrMigrationCompletionHoldLimit) || sink.Body.Len() != 0 || w.DownstreamWritten() {
			t.Fatal("oversized response was confirmed or emitted")
		}
	})
	t.Run("SSE-frame", func(t *testing.T) {
		w, sink := newMigrationCompletionTestWriter(t, MigrationCompletionResponses)
		w.options.MaxFrameBytes = 16
		n, err := w.Write([]byte(strings.Repeat("x", 10000)))
		if n != 16 || !errors.Is(err, ErrMigrationCompletionFrameLimit) || len(w.frame) != 16 || cap(w.frame) > 16 {
			t.Fatalf("frame limit: n=%d error=%v buffered=%d", n, err, len(w.frame))
		}
		if err := w.ConfirmPersisted(); !errors.Is(err, ErrMigrationCompletionFrameLimit) || sink.Body.Len() != 0 {
			t.Fatal("oversized frame escaped")
		}
	})
}

func TestMigrationCompletionAbortNeverImpliesSuccess(t *testing.T) {
	w, sink := newMigrationCompletionTestWriter(t, MigrationCompletionNonStreaming)
	migrationCompletionWrite(t, w, strings.Repeat("secret", 100))
	path := w.held.file.Name()
	persistErr := errors.New("journal unavailable")
	if err := w.Abort(persistErr); !errors.Is(err, persistErr) {
		t.Fatal(err)
	}
	w.Flush()
	w.WriteHeaderNow()
	if _, err := w.WriteString("success"); !errors.Is(err, persistErr) {
		t.Fatalf("write after abort: %v", err)
	}
	if err := w.ConfirmPersisted(); !errors.Is(err, persistErr) || w.PersistenceConfirmed() {
		t.Fatal("abort allowed confirmation")
	}
	if sink.Body.Len() != 0 || w.DownstreamWritten() {
		t.Fatal("abort emitted a fake successful body/header")
	}
	if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("aborted spool retained: %v", err)
	}
}

func TestMigrationCompletionCloseRequiresExplicitConfirmation(t *testing.T) {
	w, sink := newMigrationCompletionTestWriter(t, MigrationCompletionNonStreaming)
	migrationCompletionWrite(t, w, "success")
	if err := w.Close(); !errors.Is(err, ErrMigrationCompletionUnconfirmed) || w.DownstreamWritten() || sink.Body.Len() != 0 {
		t.Fatalf("Close completed successful output: %v", err)
	}
}

func TestMigrationCompletionRejectsMalformedOrUnsupportedStreams(t *testing.T) {
	for _, frame := range []string{
		"data: invalid JSON\n\n",
		"event: response.completed\ndata: {\"type\":\"response.output_text.delta\"}\n\n",
	} {
		w, sink := newMigrationCompletionTestWriter(t, MigrationCompletionResponses)
		if _, err := w.WriteString(frame); err == nil {
			t.Fatal("malformed stream accepted")
		}
		if err := w.ConfirmPersisted(); err == nil || sink.Body.Len() != 0 {
			t.Fatal("malformed stream confirmed")
		}
	}
	w, _ := newMigrationCompletionTestWriter(t, MigrationCompletionChat)
	if _, err := w.WriteString("data: {\"choices\":[{\"finish_reason\":{}}]}\n\n"); err == nil {
		t.Fatal("nonstring reason treated as a successful terminal")
	}
	w, _ = newMigrationCompletionTestWriter(t, MigrationCompletionResponses)
	w.Header().Set("Content-Encoding", "gzip")
	if _, err := w.WriteString("compressed"); !errors.Is(err, ErrMigrationCompletionUnsupported) {
		t.Fatalf("compressed SSE accepted: %v", err)
	}
	w, _ = newMigrationCompletionTestWriter(t, MigrationCompletionResponses)
	if _, _, err := w.Hijack(); !errors.Is(err, ErrMigrationCompletionUnsupported) {
		t.Fatalf("WebSocket/hijack accepted: %v", err)
	}
	if w.Pusher() != nil {
		t.Fatal("server push exposes a bypass")
	}
}

func TestMigrationCompletionConstructorRequiresExplicitCoverageAndLimits(t *testing.T) {
	w, _ := newMigrationCompletionTestWriter(t, MigrationCompletionNonStreaming)
	options := w.options
	for _, protocol := range []MigrationCompletionProtocol{"", "websocket", "unknown"} {
		options.Protocol = protocol
		if _, err := NewMigrationCompletionWriter(w.dst, options); !errors.Is(err, ErrMigrationCompletionUnsupported) {
			t.Fatalf("unsupported protocol %q: %v", protocol, err)
		}
	}
	options = w.options
	options.MemoryBytes = 0
	if _, err := NewMigrationCompletionWriter(w.dst, options); err == nil {
		t.Fatal("implicit memory limit accepted")
	}
	w.dst.WriteHeaderNow()
	if _, err := NewMigrationCompletionWriter(w.dst, w.options); err == nil {
		t.Fatal("installed after downstream commit")
	}
}

func TestMigrationCompletionFinalTerminalWithoutBlankLine(t *testing.T) {
	w, sink := newMigrationCompletionTestWriter(t, MigrationCompletionResponses)
	frame := "data: {\"type\":\"response.completed\"}"
	migrationCompletionWrite(t, w, frame)
	if err := w.Finish(); err != nil || !w.TerminalObserved() || sink.Body.Len() != 0 {
		t.Fatalf("EOF terminal recognition: %v", err)
	}
	if err := w.ConfirmPersisted(); err != nil || sink.Body.String() != frame {
		t.Fatalf("EOF terminal changed: %v, %q", err, sink.Body.String())
	}
}

func TestMigrationCompletionReplayFailureIsNeverRetried(t *testing.T) {
	w, sink := newMigrationCompletionTestWriter(t, MigrationCompletionNonStreaming)
	migrationCompletionWrite(t, w, "success body")
	if err := w.Finish(); err != nil {
		t.Fatal(err)
	}
	sink.writeErr, sink.writeMax = io.ErrClosedPipe, 3
	if err := w.ConfirmPersisted(); !errors.Is(err, io.ErrClosedPipe) || w.PersistenceConfirmed() || sink.Body.String() != "suc" {
		t.Fatalf("replay failure: %v, confirmed=%v body=%q", err, w.PersistenceConfirmed(), sink.Body.String())
	}
	if w.Size() != len("success body") || w.DownstreamSize() != 3 {
		t.Fatal("replay error corrupted logical or physical accounting")
	}
	if err := w.ConfirmPersisted(); !errors.Is(err, io.ErrClosedPipe) || sink.Body.String() != "suc" {
		t.Fatal("failed replay was retried")
	}
}

func TestMigrationCompletionEmptyWriteAndCloseNotify(t *testing.T) {
	w, sink := newMigrationCompletionTestWriter(t, MigrationCompletionNonStreaming)
	if n, err := w.WriteString(""); n != 0 || err != nil || !w.Written() || w.Size() != 0 || w.DownstreamWritten() {
		t.Fatalf("empty Gin write semantics: n=%d error=%v", n, err)
	}
	if w.CloseNotify() != sink.closed {
		t.Fatal("CloseNotify was not delegated")
	}
}
