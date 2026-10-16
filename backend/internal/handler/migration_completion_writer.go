package handler

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/gin-gonic/gin"
)

// This file is a default-unused migration foundation, not middleware. Wiring it
// requires the handler to call Finish, durably persist its final usage, and then
// call ConfirmPersisted BEFORE returning. Gin commits c.writermem directly on
// return: leaving an unconfirmed wrapper installed does not prevent an empty
// HTTP 200. On failure the caller must explicitly abort the HTTP response; Abort
// only discards held bytes and cannot retract already streamed headers/deltas.
// Neither EOF, Close, Flush nor a successful upstream status confirms usage.

type MigrationCompletionProtocol string

const (
	MigrationCompletionNonStreaming MigrationCompletionProtocol = "nonstream"
	MigrationCompletionResponses    MigrationCompletionProtocol = "sse-responses"
	MigrationCompletionChat         MigrationCompletionProtocol = "sse-chat"
	MigrationCompletionMessages     MigrationCompletionProtocol = "sse-messages"
)

var (
	ErrMigrationCompletionUnsupported = errors.New("migration completion writer: unsupported protocol or upgrade")
	ErrMigrationCompletionFrameLimit  = errors.New("migration completion writer: SSE frame limit exceeded")
	ErrMigrationCompletionHoldLimit   = errors.New("migration completion writer: held body limit exceeded")
	ErrMigrationCompletionNoTerminal  = errors.New("migration completion writer: no recognized successful terminal event")
	ErrMigrationCompletionUnconfirmed = errors.New("migration completion writer: persistence not confirmed")
	ErrMigrationCompletionNotFinished = errors.New("migration completion writer: Finish must precede confirmation")
	ErrMigrationCompletionFinished    = errors.New("migration completion writer: response already finished")
)

// All resource limits are required, explicit rollout choices. There are no
// production defaults: limits must preserve each endpoint's existing accepted
// response sizes, including full output embedded in response.completed. Memory
// is bounded per writer by MemoryBytes plus MaxFrameBytes and JSON decoding
// overhead; rollout must additionally bound concurrent writers and disk usage.
// SpoolDirectory must be a private, capacity-monitored local directory.
type MigrationCompletionOptions struct {
	Protocol       MigrationCompletionProtocol
	MemoryBytes    int
	MaxFrameBytes  int
	MaxHeldBytes   int64
	SpoolDirectory string
}

// MigrationCompletionWriter retains successful nonstream responses, or the
// first recognized successful SSE terminal frame and everything after it.
// Complete nonterminal SSE frames are forwarded and flushed immediately. It
// preserves Gin's LOGICAL Written/Size decisions so buffering cannot silently
// re-enable credential failover. DownstreamWritten/DownstreamSize describe the
// real client response separately. Like Gin, it requires serialized access.
//
// Only the named JSON SSE protocols, LF/CRLF framing and HTTP bodies are covered.
// Unknown protocols, bare-CR SSE, WebSockets, upgrades, compressed SSE bytes,
// HTTP server push, and concurrent writes are unsupported. Missing or malformed
// terminal events cannot be confirmed. It does not retry upstream requests.
type MigrationCompletionWriter struct {
	dst       gin.ResponseWriter
	options   MigrationCompletionOptions
	header    http.Header
	committed http.Header
	status    int
	size      int
	frame     []byte
	lineBytes int
	terminal  bool
	dataSent  bool
	finished  bool
	confirmed bool
	err       error
	held      migrationCompletionSpool
}

var _ gin.ResponseWriter = (*MigrationCompletionWriter)(nil)

func NewMigrationCompletionWriter(dst gin.ResponseWriter, options MigrationCompletionOptions) (*MigrationCompletionWriter, error) {
	if dst == nil || dst.Written() {
		return nil, errors.New("migration completion writer must be installed before the response is written")
	}
	switch options.Protocol {
	case MigrationCompletionNonStreaming, MigrationCompletionResponses, MigrationCompletionChat, MigrationCompletionMessages:
	default:
		return nil, ErrMigrationCompletionUnsupported
	}
	if options.MemoryBytes <= 0 || options.MaxFrameBytes <= 0 || options.MaxHeldBytes <= 0 || int64(options.MemoryBytes) > options.MaxHeldBytes {
		return nil, errors.New("migration completion writer requires explicit positive, consistent resource limits")
	}
	if !filepath.IsAbs(options.SpoolDirectory) {
		return nil, errors.New("migration completion spool directory must be absolute")
	}
	info, err := os.Stat(options.SpoolDirectory)
	if err != nil || !info.IsDir() {
		return nil, errors.New("migration completion spool directory is unavailable")
	}
	return &MigrationCompletionWriter{
		dst: dst, options: options, header: dst.Header().Clone(), status: dst.Status(), size: -1,
		held: migrationCompletionSpool{memoryLimit: options.MemoryBytes, limit: options.MaxHeldBytes, directory: options.SpoolDirectory},
	}, nil
}

func (w *MigrationCompletionWriter) Header() http.Header { return w.header }
func (w *MigrationCompletionWriter) Status() int         { return w.status }
func (w *MigrationCompletionWriter) Size() int           { return w.size }
func (w *MigrationCompletionWriter) Written() bool       { return w.size >= 0 }

func (w *MigrationCompletionWriter) DownstreamWritten() bool    { return w.dst.Written() }
func (w *MigrationCompletionWriter) DownstreamSize() int        { return w.dst.Size() }
func (w *MigrationCompletionWriter) TerminalObserved() bool     { return w.terminal }
func (w *MigrationCompletionWriter) PersistenceConfirmed() bool { return w.confirmed }
func (w *MigrationCompletionWriter) Err() error                 { return w.err }

// StreamDataForwarded excludes headers and comment-only heartbeat frames. It
// is evidence of forwarded nonterminal data, not gateway readiness or billing.
func (w *MigrationCompletionWriter) StreamDataForwarded() bool { return w.dataSent }

func (w *MigrationCompletionWriter) WriteHeader(code int) {
	if code > 0 && !w.Written() {
		w.status = code
	}
}

func (w *MigrationCompletionWriter) successful() bool { return w.status >= 200 && w.status < 300 }
func (w *MigrationCompletionWriter) streaming() bool {
	return w.options.Protocol != MigrationCompletionNonStreaming
}

func (w *MigrationCompletionWriter) fail(err error) error {
	if w.err == nil {
		w.err = err
	}
	return w.err
}

func (w *MigrationCompletionWriter) logicalHeader() {
	if !w.Written() {
		w.size = 0
		w.committed = w.header.Clone()
	}
}

func (w *MigrationCompletionWriter) downstreamHeader() {
	if w.dst.Written() {
		return
	}
	header := w.dst.Header()
	for key := range header {
		delete(header, key)
	}
	for key, values := range w.committed {
		header[key] = append([]string(nil), values...)
	}
	w.dst.WriteHeader(w.status)
	w.dst.WriteHeaderNow()
}

func (w *MigrationCompletionWriter) WriteHeaderNow() {
	if w.err != nil || w.finished {
		return
	}
	w.logicalHeader()
	if w.status < 200 {
		w.fail(ErrMigrationCompletionUnsupported)
		return
	}
	if w.successful() && w.streaming() {
		encoding := strings.TrimSpace(w.committed.Get("Content-Encoding"))
		if encoding != "" && !strings.EqualFold(encoding, "identity") {
			w.fail(ErrMigrationCompletionUnsupported)
			return
		}
	}
	// Streaming headers can precede usage persistence; a terminal success
	// signal cannot. Nonstream success headers stay held with their body.
	if !w.successful() || w.streaming() {
		w.downstreamHeader()
	}
}

func (w *MigrationCompletionWriter) Write(p []byte) (int, error) {
	if w.err != nil {
		return 0, w.err
	}
	if w.finished {
		return 0, ErrMigrationCompletionFinished
	}
	w.WriteHeaderNow()
	if w.err != nil {
		return 0, w.err
	}
	var n int
	var err error
	switch {
	case !w.successful():
		n, err = w.dst.Write(p)
	case !w.streaming() || w.terminal:
		n, err = w.held.Write(p)
	default:
		n, err = w.writeSSE(p)
	}
	w.size += n
	if err != nil {
		return n, w.fail(err)
	}
	return n, nil
}

func (w *MigrationCompletionWriter) WriteString(s string) (int, error) {
	// Avoid duplicating a potentially huge nonstream string in memory.
	total := 0
	for len(s) > 0 {
		chunk := min(len(s), 16*1024)
		n, err := w.Write([]byte(s[:chunk]))
		total += n
		if err != nil {
			return total, err
		}
		s = s[chunk:]
	}
	if total == 0 {
		return w.Write(nil)
	}
	return total, nil
}

func (w *MigrationCompletionWriter) Flush() {
	if w.err != nil || w.finished {
		return
	}
	w.WriteHeaderNow()
	if w.err == nil && w.dst.Written() && (!w.successful() || (w.streaming() && !w.terminal)) {
		w.dst.Flush()
	}
}

func (w *MigrationCompletionWriter) CloseNotify() <-chan bool { return w.dst.CloseNotify() }
func (w *MigrationCompletionWriter) Pusher() http.Pusher      { return nil }
func (w *MigrationCompletionWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	return nil, nil, ErrMigrationCompletionUnsupported
}

func (w *MigrationCompletionWriter) writeSSE(p []byte) (int, error) {
	for i, b := range p {
		if len(w.frame) >= w.options.MaxFrameBytes {
			return i, ErrMigrationCompletionFrameLimit
		}
		w.frame = migrationCompletionAppend(w.frame, []byte{b}, w.options.MaxFrameBytes)
		if b != '\n' {
			w.lineBytes++
			continue
		}
		blank := w.lineBytes == 0 || (w.lineBytes == 1 && len(w.frame) >= 2 && w.frame[len(w.frame)-2] == '\r')
		w.lineBytes = 0
		if !blank {
			continue
		}
		if err := w.dispatchFrame(); err != nil {
			return i + 1, err
		}
		if w.terminal {
			n, err := w.held.Write(p[i+1:])
			return i + 1 + n, err
		}
	}
	return len(p), nil
}

func (w *MigrationCompletionWriter) dispatchFrame() error {
	terminal, hasData, err := migrationSSETerminal(w.options.Protocol, w.frame)
	if err != nil {
		return err
	}
	if terminal {
		w.terminal = true
		_, err = w.held.Write(w.frame)
	} else {
		var n int
		n, err = w.dst.Write(w.frame)
		if err == nil && n != len(w.frame) {
			err = io.ErrShortWrite
		}
		if err == nil {
			w.dataSent = w.dataSent || hasData
			w.dst.Flush()
		}
	}
	w.frame = w.frame[:0]
	return err
}

// Finish closes logical writes and validates completion. It never confirms
// persistence or sends a held success. A final SSE frame without a blank line
// is recognized but preserved byte-for-byte if later explicitly confirmed.
func (w *MigrationCompletionWriter) Finish() error {
	if w.finished {
		return w.err
	}
	if w.err == nil {
		w.WriteHeaderNow()
	}
	w.finished = true
	if w.err != nil || !w.successful() {
		return w.err
	}
	if w.streaming() && !w.terminal {
		if len(w.frame) > 0 {
			terminal, _, err := migrationSSETerminal(w.options.Protocol, w.frame)
			if err != nil {
				return w.fail(err)
			}
			if terminal {
				w.terminal = true
				if _, err := w.held.Write(w.frame); err != nil {
					return w.fail(err)
				}
				w.frame = nil
			}
		}
		if !w.terminal {
			return w.fail(ErrMigrationCompletionNoTerminal)
		}
	}
	return nil
}

// ConfirmPersisted is the only method that releases a successful completion.
// The caller, not this writer, must prove final usage is durable. It must be
// invoked after Finish. Downstream write errors remain errors and are not retried.
func (w *MigrationCompletionWriter) ConfirmPersisted() error {
	if w.err != nil {
		return w.err
	}
	if !w.finished {
		return ErrMigrationCompletionNotFinished
	}
	if w.confirmed {
		return nil
	}
	if !w.successful() {
		return nil
	}
	w.downstreamHeader()
	if err := w.held.Replay(w.dst); err != nil {
		return w.fail(err)
	}
	if w.streaming() {
		w.dst.Flush()
	}
	w.confirmed = true
	return w.held.Close()
}

// Abort discards held output without emitting a successful terminal signal. It
// does not complete/close the network response. Callers must handle Gin's final
// implicit header commit and already streamed responses explicitly (see above).
func (w *MigrationCompletionWriter) Abort(cause error) error {
	if cause == nil {
		cause = ErrMigrationCompletionUnconfirmed
	}
	w.fail(cause)
	w.finished = true
	w.frame = nil
	return errors.Join(w.err, w.held.Close())
}

// Close releases private spool resources. It deliberately does not imply
// success; dropping an unconfirmed successful response returns an error.
func (w *MigrationCompletionWriter) Close() error {
	if w.successful() && !w.confirmed && w.err == nil {
		w.fail(ErrMigrationCompletionUnconfirmed)
	}
	w.finished = true
	w.frame = nil
	return errors.Join(w.err, w.held.Close())
}

func migrationSSETerminal(protocol MigrationCompletionProtocol, frame []byte) (terminal, hasData bool, err error) {
	var event string
	var data bytes.Buffer
	for _, line := range bytes.Split(frame, []byte{'\n'}) {
		line = bytes.TrimSuffix(line, []byte{'\r'})
		if len(line) == 0 || line[0] == ':' {
			continue
		}
		key, value, _ := bytes.Cut(line, []byte{':'})
		value = bytes.TrimPrefix(value, []byte{' '})
		switch string(key) {
		case "event":
			event = string(value)
		case "data":
			if hasData {
				data.WriteByte('\n')
			}
			data.Write(value)
			hasData = true
		}
	}
	if !hasData {
		return false, false, nil
	}
	payload := bytes.TrimSpace(data.Bytes())
	if bytes.Equal(payload, []byte("[DONE]")) {
		return true, true, nil
	}
	var value struct {
		Type    string          `json:"type"`
		Delta   json.RawMessage `json:"delta"`
		Choices json.RawMessage `json:"choices"`
	}
	if err := json.Unmarshal(payload, &value); err != nil {
		return false, true, fmt.Errorf("migration completion writer: invalid JSON SSE frame: %w", err)
	}
	if value.Type != "" && event != "" && value.Type != event {
		return false, true, errors.New("migration completion writer: conflicting SSE event and data type")
	}
	if value.Type != "" {
		event = value.Type
	}
	switch protocol {
	case MigrationCompletionResponses:
		return event == "response.completed" || event == "response.done", true, nil
	case MigrationCompletionMessages:
		if event == "message_stop" {
			return true, true, nil
		}
		if event == "message_delta" {
			var delta struct {
				StopReason json.RawMessage `json:"stop_reason"`
			}
			if err := json.Unmarshal(value.Delta, &delta); err != nil {
				return false, true, fmt.Errorf("migration completion writer: invalid message delta: %w", err)
			}
			terminal, err := migrationNonNullReason(delta.StopReason)
			return terminal, true, err
		}
		return false, true, nil
	case MigrationCompletionChat:
		var choices []struct {
			FinishReason json.RawMessage `json:"finish_reason"`
		}
		if len(value.Choices) > 0 {
			if err := json.Unmarshal(value.Choices, &choices); err != nil {
				return false, true, fmt.Errorf("migration completion writer: invalid chat choices: %w", err)
			}
		}
		for _, choice := range choices {
			terminal, err := migrationNonNullReason(choice.FinishReason)
			if err != nil {
				return false, true, err
			}
			if terminal {
				return true, true, nil
			}
		}
		return false, true, nil
	default:
		return false, true, ErrMigrationCompletionUnsupported
	}
}

func migrationNonNullReason(raw json.RawMessage) (bool, error) {
	value := bytes.TrimSpace(raw)
	if len(value) == 0 || bytes.Equal(value, []byte("null")) {
		return false, nil
	}
	var reason string
	if err := json.Unmarshal(value, &reason); err != nil {
		return false, errors.New("migration completion writer: terminal reason is not a string")
	}
	return reason != "", nil
}

type migrationCompletionSpool struct {
	memoryLimit int
	limit       int64
	directory   string
	memory      []byte
	file        *os.File
	size        int64
}

func (s *migrationCompletionSpool) Write(p []byte) (int, error) {
	allowed := int64(len(p))
	var limitErr error
	if allowed > s.limit-s.size {
		allowed = s.limit - s.size
		limitErr = ErrMigrationCompletionHoldLimit
	}
	p = p[:int(allowed)]
	if s.file == nil && int64(len(s.memory))+allowed > int64(s.memoryLimit) {
		file, err := os.CreateTemp(s.directory, ".sub2api-migration-completion-*")
		if err != nil {
			return 0, err
		}
		s.file = file // CreateTemp creates mode 0600.
		if n, err := file.Write(s.memory); err != nil {
			return 0, err
		} else if n != len(s.memory) {
			return 0, io.ErrShortWrite
		}
		s.memory = nil
	}
	var n int
	var err error
	if s.file != nil {
		n, err = s.file.Write(p)
	} else {
		s.memory = migrationCompletionAppend(s.memory, p, s.memoryLimit)
		n = len(p)
	}
	s.size += int64(n)
	if err != nil {
		return n, err
	}
	if n != len(p) {
		return n, io.ErrShortWrite
	}
	return n, limitErr
}

func (s *migrationCompletionSpool) Replay(dst io.Writer) error {
	if s.file == nil {
		_, err := io.Copy(dst, bytes.NewReader(s.memory))
		return err
	}
	if _, err := s.file.Seek(0, io.SeekStart); err != nil {
		return err
	}
	_, err := io.Copy(dst, s.file)
	return err
}

func (s *migrationCompletionSpool) Close() error {
	s.memory = nil
	if s.file == nil {
		return nil
	}
	name := s.file.Name()
	err := s.file.Close()
	s.file = nil
	return errors.Join(err, os.Remove(name))
}

// Unlike append's automatic growth, this keeps the backing allocation within
// the caller's explicit byte budget. Callers check the prospective length first.
func migrationCompletionAppend(dst, p []byte, limit int) []byte {
	needed := len(dst) + len(p)
	if needed > cap(dst) {
		capacity := min(limit, max(needed, max(64, cap(dst)*2)))
		next := make([]byte, len(dst), capacity)
		copy(next, dst)
		dst = next
	}
	return append(dst, p...)
}
