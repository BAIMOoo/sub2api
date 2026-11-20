//go:build codexlive

package integration_test

import (
	"bytes"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/Wei-Shaw/sub2api/internal/service"
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
	resp, err := u.upstream.DoWithTLS(req, proxy, accountID, concurrency, profile)
	if err != nil || resp == nil || resp.StatusCode != http.StatusOK || resp.Body == nil {
		return resp, err
	}
	wireMode := "Standard"
	if req.Header.Get("x-openai-internal-codex-responses-lite") == "true" {
		wireMode = "Lite"
	}
	u.evidence = &codexLiveResponseEvidence{wireMode: wireMode}
	resp.Body = struct {
		io.Reader
		io.Closer
	}{io.TeeReader(resp.Body, u.evidence), resp.Body}
	return resp, nil
}

// Keep a bounded, in-memory copy of bytes consumed by the gateway. Do not
// log it: the downstream JSON shape alone cannot prove upstream completion.
type codexLiveResponseEvidence struct {
	wireMode string
	mu       sync.Mutex
	body     bytes.Buffer
	overflow bool
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
