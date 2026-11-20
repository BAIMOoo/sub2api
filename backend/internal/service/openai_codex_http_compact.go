package service

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/util/responseheaders"
	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

func isCodexHTTPCompactRequest(c *gin.Context, account *Account) bool {
	return isOpenAIResponsesCompactPath(c) && codexHTTPPolicyForRequest(c, account).usesFixedExecContract()
}

func (policy codexHTTPPolicy) responsesUpstreamURL(c *gin.Context, baseURL string) string {
	if policy.usesFixedExecContract() && isOpenAIResponsesCompactPath(c) {
		return baseURL
	}
	return appendOpenAIResponsesRequestPathSuffix(baseURL, openAIResponsesRequestPathSuffix(c))
}

func setCodexHTTPCompactionTrigger(request map[string]any) error {
	input, ok := request["input"].([]any)
	if !ok {
		return newOpenAIResponsesLiteValidationError("input", "compact requires a conversation")
	}
	items := make([]any, 0, len(input)+1)
	for _, raw := range input {
		item, _ := raw.(map[string]any)
		if item["type"] == "compaction_trigger" {
			if len(item) != 1 {
				return newOpenAIResponsesLiteValidationError("input", "compaction trigger fields cannot be converted losslessly")
			}
			continue
		}
		items = append(items, raw)
	}
	request["input"] = append(items, map[string]any{"type": "compaction_trigger"})
	return nil
}

func decodeCodexHTTPCompactCompletion(terminalType string, terminalPayload []byte, stream string, request []byte) ([]byte, error) {
	if terminalType != "response.completed" {
		return nil, errors.New("upstream compaction stream did not complete")
	}
	if !gjson.ValidBytes(terminalPayload) {
		return nil, errors.New("upstream compaction returned a malformed completion event")
	}
	response := gjson.GetBytes(terminalPayload, "response")
	id, status := response.Get("id"), response.Get("status")
	if !response.IsObject() || id.Type != gjson.String || strings.TrimSpace(id.String()) == "" || !response.Get("usage").IsObject() ||
		(status.Exists() && status.String() != "completed") {
		return nil, errors.New("upstream compaction returned an invalid completed response")
	}

	var completed, malformed bool
	var compactItems []json.RawMessage
	forEachOpenAISSEFrame(stream, func(eventType string, payload []byte) {
		if !gjson.ValidBytes(payload) || !gjson.ParseBytes(payload).IsObject() {
			malformed = true
			return
		}
		switch eventType {
		case "response.completed":
			malformed = malformed || completed
			completed = true
		case "response.output_item.done":
			item := gjson.GetBytes(payload, "item")
			if completed || !item.IsObject() || item.Get("type").Type != gjson.String {
				malformed = true
				return
			}
			if !isResponsesCompactionItemType(item.Get("type").String()) {
				return
			}
			content := item.Get("encrypted_content")
			if content.Type != gjson.String || content.String() == "" {
				malformed = true
				return
			}
			compactItems = append(compactItems, json.RawMessage(item.Raw))
		}
	})
	if malformed {
		return nil, errors.New("upstream compaction returned a malformed event")
	}
	if len(compactItems) != 1 {
		return nil, errors.New("upstream compaction requires exactly one completed compaction item")
	}

	// V2 returns only the summary item; legacy clients replace their replay
	// history with output, which also includes the retained user messages.
	var output []json.RawMessage
	for _, item := range gjson.GetBytes(request, "input").Array() {
		itemType := item.Get("type")
		if item.Get("role").String() != "user" || (itemType.Exists() && itemType.String() != "message") {
			continue
		}
		raw := []byte(item.Raw)
		var err error
		if !itemType.Exists() {
			raw, err = sjson.SetBytes(raw, "type", "message")
			if err != nil {
				return nil, err
			}
		}
		if content := item.Get("content"); content.Type == gjson.String {
			raw, err = sjson.SetBytes(raw, "content", []map[string]string{{"type": "input_text", "text": content.String()}})
			if err != nil {
				return nil, err
			}
		}
		output = append(output, raw)
	}
	body, err := sjson.SetBytes([]byte(response.Raw), "output", append(output, compactItems[0]))
	if err != nil {
		return nil, err
	}
	if response.Get("created_at").Int() <= 0 {
		body, err = sjson.SetBytes(body, "created_at", time.Now().Unix())
		if err != nil {
			return nil, err
		}
	}
	return sjson.SetBytes(body, "object", "response.compaction")
}

func (s *OpenAIGatewayService) handleCodexHTTPCompactResponse(ctx context.Context, resp *http.Response, c *gin.Context, account *Account, request []byte, originalModel, mappedModel string, passthrough bool) (*openaiNonStreamingResult, error) {
	raw, err := ReadUpstreamResponseBody(resp.Body, s.cfg, c, openAITooLargeError)
	if err != nil {
		return nil, err
	}
	stream := string(raw)
	observer := upstreamResponseModelObserverFromContext(c)
	if observer == nil {
		observer = beginUpstreamResponseModelObservation(c)
	}
	observeOpenAISSEBody(observer, stream)

	terminalType, terminalPayload, _ := extractOpenAISSETerminalEvent(stream)
	if terminalType == "response.failed" || terminalType == "error" {
		return nil, s.handleOpenAIUnarySSEFailure(resp, c, account, terminalType, terminalPayload, mappedModel, passthrough)
	}
	body, err := decodeCodexHTTPCompactCompletion(terminalType, terminalPayload, stream, request)
	if err != nil {
		return nil, s.writeOpenAINonStreamingProtocolError(resp, c, err.Error())
	}
	if mappedModel != "" && originalModel != mappedModel {
		body = s.replaceModelInResponseBody(body, mappedModel, originalModel)
	}
	usage, _ := extractOpenAIUsageFromJSONBytes(body)
	logOpenAISuccessMissingUsage(ctx, c, account, resp, &usage, terminalType, false)
	if passthrough {
		writeOpenAIPassthroughResponseHeaders(c.Writer.Header(), resp.Header, s.responseHeaderFilter)
	} else {
		responseheaders.WriteFilteredHeaders(c.Writer.Header(), resp.Header, s.responseHeaderFilter)
	}
	s.relayOpenAICodexTurnState(c, account, resp.Header)
	if !writeOpenAICompactSSEBridge(c, resp.StatusCode, body) {
		const contentType = "application/json; charset=utf-8"
		c.Writer.Header().Set("Content-Type", contentType)
		c.Data(resp.StatusCode, contentType, body)
	}
	return &openaiNonStreamingResult{OpenAIUsage: &usage, usage: &usage, responseID: extractOpenAIResponseIDFromJSONBytes(body)}, nil
}
