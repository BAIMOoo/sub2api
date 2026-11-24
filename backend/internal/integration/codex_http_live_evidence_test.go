//go:build codexlive

package integration_test

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/klauspost/compress/zstd"
)

const codexLiveEvidenceLimit = 1 << 20

type codexLiveUpstream struct {
	upstream service.HTTPUpstream
	evidence *codexLiveResponseEvidence
}

func (*codexLiveUpstream) Do(*http.Request, string, int64, int) (*http.Response, error) {
	return nil, errors.New("live verification requires the fixed Codex HTTP transport")
}

func (u *codexLiveUpstream) DoWithTLS(req *http.Request, proxy string, accountID int64, concurrency int, profile *tlsfingerprint.Profile) (*http.Response, error) {
	u.evidence = nil
	if profile == nil || service.HTTPUpstreamProfileFromContext(req.Context()) != service.HTTPUpstreamProfileCodexHTTP ||
		req.URL.String() != "https://chatgpt.com/backend-api/codex/responses" {
		return nil, errors.New("unexpected live verification transport or endpoint")
	}
	wireMode := "Standard"
	if req.Header.Get("x-openai-internal-codex-responses-lite") == "true" {
		wireMode = "Lite"
	}
	requestReasoningIDs := extractCodexLiveZstdRequestReasoningIDs(req)
	resp, err := u.upstream.DoWithTLS(req, proxy, accountID, concurrency, profile)
	if err != nil || resp == nil || resp.StatusCode != http.StatusOK || resp.Body == nil {
		return resp, err
	}
	u.evidence = &codexLiveResponseEvidence{
		wireMode:            wireMode,
		requestReasoningIDs: requestReasoningIDs,
	}
	resp.Body = struct {
		io.Reader
		io.Closer
	}{io.TeeReader(resp.Body, u.evidence), resp.Body}
	return resp, nil
}

// Keep a bounded, in-memory copy of bytes consumed by the gateway. Do not
// log it: the downstream JSON shape alone cannot prove upstream completion.
type codexLiveResponseEvidence struct {
	wireMode            string
	requestReasoningIDs []string
	mu                  sync.Mutex
	body                bytes.Buffer
	overflow            bool
}

func (e *codexLiveResponseEvidence) replayedAllReasoningIDs(expected []string) bool {
	if e == nil || len(expected) == 0 {
		return false
	}
	for _, id := range expected {
		if !slices.Contains(e.requestReasoningIDs, id) {
			return false
		}
	}
	return true
}

func extractCodexLiveZstdRequestReasoningIDs(req *http.Request) []string {
	if req == nil || req.GetBody == nil || req.Header.Get("Content-Encoding") != "zstd" {
		return nil
	}
	rc, err := req.GetBody()
	if err != nil || rc == nil {
		return nil
	}
	defer func() { _ = rc.Close() }()
	compressed, err := io.ReadAll(io.LimitReader(rc, codexLiveEvidenceLimit+1))
	if err != nil || len(compressed) == 0 || len(compressed) > codexLiveEvidenceLimit {
		return nil
	}
	decoder, err := zstd.NewReader(bytes.NewReader(compressed))
	if err != nil {
		return nil
	}
	defer decoder.Close()
	decoded, err := io.ReadAll(io.LimitReader(decoder, codexLiveEvidenceLimit+1))
	if err != nil || len(decoded) > codexLiveEvidenceLimit {
		return nil
	}
	var payload struct {
		Input []struct {
			Type             string `json:"type"`
			ID               string `json:"id"`
			EncryptedContent string `json:"encrypted_content"`
		} `json:"input"`
	}
	if json.Unmarshal(decoded, &payload) != nil {
		return nil
	}
	var ids []string
	for _, item := range payload.Input {
		if item.Type == "reasoning" && strings.TrimSpace(item.ID) != "" && strings.TrimSpace(item.EncryptedContent) != "" {
			ids = append(ids, strings.TrimSpace(item.ID))
		}
	}
	return ids
}

func (e *codexLiveResponseEvidence) Write(p []byte) (int, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	n := min(len(p), codexLiveEvidenceLimit-e.body.Len())
	e.overflow = e.overflow || n != len(p)
	_, _ = e.body.Write(p[:n])
	return len(p), nil
}

func (e *codexLiveResponseEvidence) completed() bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	return !e.overflow && service.CodexLiveSSECompleted(e.body.String())
}

func TestCodexLiveRequiresUpstreamCompletionEvidence(t *testing.T) {
	const completeLine = "data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\"}}"
	const complete = completeLine + "\r\n\r\n"
	const incomplete = "data: {\"type\":\"response.incomplete\",\"response\":{\"status\":\"incomplete\"}}\n\n"
	const namedIncomplete = "event: response.incomplete\ndata: {\"response\":{\"status\":\"incomplete\"}}\n\n"
	for _, tc := range []struct {
		name, stream string
		completed    bool
	}{
		{"completed", complete + "data: [DONE]\n\n", true},
		{"named multiline completion", "event: response.completed\r\ndata: {\"response\":\r\ndata:  {\"status\":\"completed\"}}\r\n\r\n", true},
		{"incomplete", incomplete, false},
		{"named incomplete", namedIncomplete, false},
		{"missing status", "data: {\"type\":\"response.completed\"}\n\n", false},
		{"missing event", "data: [DONE]\n\n", false},
		{"completion after done", "data: [DONE]\n\n" + complete, false},
		{"conflicting terminal", complete + incomplete, false},
		{"case-insensitive incomplete before completion", strings.Replace(incomplete, `"type"`, `"Type"`, 1) + complete, false},
		{"malformed completed frame then named incomplete", completeLine + "\ndata: {}\n\n" + namedIncomplete, false},
		{"oversize", complete + strings.Repeat("x", codexLiveEvidenceLimit), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var evidence codexLiveResponseEvidence
			if _, err := io.WriteString(&evidence, tc.stream); err != nil {
				t.Fatal(err)
			}
			if evidence.completed() != tc.completed {
				t.Fatalf("completed = %t, want %t", !tc.completed, tc.completed)
			}
		})
	}
}

func TestCodexLiveZstdRequestReasoningReplayEvidence(t *testing.T) {
	encoder, err := zstd.NewWriter(nil)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = encoder.Close() }()

	buildReq := func(raw string) *http.Request {
		compressed := encoder.EncodeAll([]byte(raw), nil)
		req, reqErr := http.NewRequest(http.MethodPost, "https://chatgpt.com/backend-api/codex/responses", bytes.NewReader(compressed))
		if reqErr != nil {
			t.Fatal(reqErr)
		}
		req.Header.Set("Content-Encoding", "zstd")
		req.GetBody = func() (io.ReadCloser, error) {
			return io.NopCloser(bytes.NewReader(compressed)), nil
		}
		return req
	}

	req := buildReq(`{"input":[{"type":"reasoning","id":"rs_keep_1","encrypted_content":"opaque-enc"},{"type":"reasoning","id":"rs_no_enc"},{"type":"reasoning","encrypted_content":"missing-id"}]}`)
	evidence := &codexLiveResponseEvidence{
		requestReasoningIDs: extractCodexLiveZstdRequestReasoningIDs(req),
	}
	if !evidence.replayedAllReasoningIDs([]string{"rs_keep_1"}) {
		t.Fatalf("expected rs_keep_1 with encrypted_content to be observed on wire, got %v", evidence.requestReasoningIDs)
	}
	if evidence.replayedAllReasoningIDs([]string{"rs_keep_1", "rs_no_enc"}) {
		t.Fatalf("reasoning item without encrypted_content must not count as replayed, got %v", evidence.requestReasoningIDs)
	}
	if evidence.replayedAllReasoningIDs(nil) {
		t.Fatal("empty expected reasoning ID list must not pass replay check")
	}
}
