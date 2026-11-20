//go:build codexlive

package integration_test

import (
	"bytes"
	"encoding/json"
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
	if e.overflow {
		return false
	}
	completed := false
	for line := range bytes.Lines(e.body.Bytes()) {
		data, ok := bytes.CutPrefix(line, []byte("data:"))
		if !ok {
			continue
		}
		data = bytes.TrimSpace(data)
		if len(data) == 0 || bytes.Equal(data, []byte("[DONE]")) {
			continue
		}
		var event struct {
			Type     string `json:"type"`
			Response struct {
				Status string `json:"status"`
			} `json:"response"`
		}
		if json.Unmarshal(data, &event) != nil {
			return false
		}
		switch event.Type {
		case "response.completed":
			if completed || event.Response.Status != "completed" {
				return false
			}
			completed = true
		case "response.incomplete", "response.failed", "response.cancelled", "response.canceled", "error":
			return false
		}
	}
	return completed
}

func TestCodexLiveRequiresUpstreamCompletionEvidence(t *testing.T) {
	const complete = "data: {\"type\":\"response.completed\",\"response\":{\"status\":\"completed\"}}\r\n\r\n"
	const incomplete = "data: {\"type\":\"response.incomplete\",\"response\":{\"status\":\"incomplete\"}}\n\n"
	for _, tc := range []struct {
		name, stream string
		completed    bool
	}{
		{"completed", complete + "data: [DONE]\n\n", true},
		{"incomplete", incomplete, false},
		{"missing status", "data: {\"type\":\"response.completed\"}\n\n", false},
		{"missing event", "data: [DONE]\n\n", false},
		{"conflicting terminal", complete + incomplete, false},
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
