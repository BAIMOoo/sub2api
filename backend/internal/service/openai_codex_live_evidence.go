//go:build codexlive

package service

import (
	"encoding/json"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/apicompat"
)

// CodexLiveSSECompleted verifies a captured provider stream for the opt-in
// acceptance test. Use the same SSE frame boundary as the compatibility routes.
func CodexLiveSSECompleted(body string) bool {
	var parser openAICompatSSEFrameParser
	completed := false
	check := func(frame openAICompatSSEFrame, ok bool) bool {
		if !ok {
			return true
		}
		data := strings.TrimSpace(frame.Data)
		if data == "" {
			return true
		}
		if data == "[DONE]" {
			return completed
		}
		if !json.Valid([]byte(data)) {
			return false
		}
		var event apicompat.ResponsesStreamEvent
		payload := openAICompatPayloadWithEventType(data, frame.EventType)
		if json.Unmarshal([]byte(payload), &event) != nil {
			return false
		}
		switch strings.TrimSpace(event.Type) {
		case "response.completed":
			if completed || event.Response == nil || event.Response.Status != "completed" {
				return false
			}
			completed = true
		case "response.done", "response.incomplete", "response.failed", "response.cancelled", "response.canceled", "error":
			return false
		}
		return true
	}
	for line := range strings.SplitSeq(body, "\n") {
		if !check(parser.AddLine(strings.TrimSuffix(line, "\r"))) {
			return false
		}
	}
	return check(parser.Finish()) && completed
}
