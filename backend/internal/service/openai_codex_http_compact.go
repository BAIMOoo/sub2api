package service

import (
	"encoding/json"
	"errors"
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

func isCodexHTTPCompactRequest(c *gin.Context, account *Account) bool {
	return isOpenAIResponsesCompactPath(c) && codexHTTPPolicyForRequest(c, account).usesFixedExecContract()
}

func openAIHTTPResponsesUpstreamURL(c *gin.Context, account *Account, baseURL string) string {
	if isCodexHTTPCompactRequest(c, account) {
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

func decodeCodexHTTPCompactCompletion(terminalType string, terminalPayload []byte, stream string) ([]byte, error) {
	if terminalType != "response.completed" && terminalType != "response.done" {
		return nil, errors.New("upstream compaction stream did not complete")
	}
	if !gjson.ValidBytes(terminalPayload) {
		return nil, errors.New("upstream compaction returned a malformed completion event")
	}
	response := gjson.GetBytes(terminalPayload, "response")
	id, status := response.Get("id"), response.Get("status")
	if !response.IsObject() || id.Type != gjson.String || strings.TrimSpace(id.String()) == "" ||
		(status.Exists() && status.String() != "completed") {
		return nil, errors.New("upstream compaction returned an invalid completed response")
	}
	output := response.Get("output")
	if output.Exists() && output.Type != gjson.Null && !output.IsArray() {
		return nil, errors.New("upstream compaction returned an invalid output array")
	}

	var done []gjson.Result
	malformed := false
	forEachOpenAISSEFrame(stream, func(eventType string, payload []byte) {
		if !gjson.ValidBytes(payload) || !gjson.ParseBytes(payload).IsObject() {
			malformed = true
			return
		}
		if eventType == "response.output_item.done" {
			item := gjson.GetBytes(payload, "item")
			if !item.IsObject() {
				malformed = true
				return
			}
			done = append(done, item)
		}
	})
	if malformed {
		return nil, errors.New("upstream compaction returned a malformed event")
	}

	compactionItems := 0
	items := output.Array()
	if len(items) == 0 {
		items = done
	} else if !responsesOutputHasCompactionItem([]byte(response.Raw)) {
		for _, item := range done {
			if isResponsesCompactionItemType(item.Get("type").String()) {
				items = append(items, item)
			}
		}
	}
	rawItems := make([]json.RawMessage, 0, len(items))
	for _, item := range items {
		if !item.IsObject() || item.Get("type").Type != gjson.String {
			return nil, errors.New("upstream compaction returned an invalid output item")
		}
		rawItems = append(rawItems, json.RawMessage(item.Raw))
		if !isResponsesCompactionItemType(item.Get("type").String()) {
			continue
		}
		content := item.Get("encrypted_content")
		if content.Type != gjson.String || content.String() == "" {
			return nil, errors.New("upstream compaction output is missing encrypted content")
		}
		compactionItems++
	}
	if compactionItems != 1 {
		return nil, errors.New("upstream compaction response must contain exactly one compaction output")
	}
	body, err := sjson.SetBytes([]byte(response.Raw), "output", rawItems)
	if err != nil {
		return nil, err
	}
	return sjson.SetBytes(body, "object", "response.compaction")
}
