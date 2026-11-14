package service

import (
	"bytes"
	"fmt"
	"strings"

	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// The ChatGPT internal Codex endpoint rejects a request whose input replays a
// hosted web_search_call item unless the request also declares the web_search
// tool; the stream then fails with "response protection is unavailable".
// Codex's local context compaction sends the full history with tools:[], so
// every compaction after a web search fails (#7927).
//
// Declare a cached-only web_search tool for such requests. Responses Lite
// rejects hosted tools at the top level, so Lite requests carry it in an
// input additional_tools item instead. When the caller declared no tools at
// all, also pin tool_choice to "none" so the injected tool cannot be invoked
// and the request keeps its no-tools semantics.

const (
	openAIWebSearchCallItemType      = "web_search_call"
	openAIAdditionalToolsItemType    = "additional_tools"
	openAIAdditionalToolsDefaultRole = "developer"
)

var openAIWebSearchHistoryTool = map[string]any{
	"type":                "web_search",
	"external_web_access": false,
}

type openAIWebSearchHistoryScan struct {
	hasWebSearchCall    bool
	callerDeclaredTools bool
	additionalToolsAt   int
}

type openAIWebSearchHistoryInjection uint8

const (
	openAIWebSearchAppendTopLevel openAIWebSearchHistoryInjection = iota
	openAIWebSearchCreateTopLevel
	openAIWebSearchAppendAdditional
	openAIWebSearchCreateAdditional
	openAIWebSearchCreateAdditionalPrefix
)

func newOpenAIWebSearchHistoryScan() openAIWebSearchHistoryScan {
	return openAIWebSearchHistoryScan{additionalToolsAt: -1}
}

func (scan *openAIWebSearchHistoryScan) observeItem(
	index int,
	itemType string,
	responsesLite bool,
	itemToolCount int,
	itemHasWebSearch bool,
) bool {
	switch strings.TrimSpace(itemType) {
	case openAIWebSearchCallItemType:
		scan.hasWebSearchCall = true
	case openAIAdditionalToolsItemType:
		if responsesLite && itemHasWebSearch {
			return false
		}
		if responsesLite && itemToolCount > 0 {
			scan.callerDeclaredTools = true
		}
		if scan.additionalToolsAt < 0 {
			scan.additionalToolsAt = index
		}
	}
	return true
}

func (scan openAIWebSearchHistoryScan) injectionPlan(
	responsesLite bool,
	hasTopLevelTools bool,
	additionalToolsArray bool,
) openAIWebSearchHistoryInjection {
	if !responsesLite {
		if hasTopLevelTools {
			return openAIWebSearchAppendTopLevel
		}
		return openAIWebSearchCreateTopLevel
	}
	switch {
	case scan.additionalToolsAt < 0:
		return openAIWebSearchCreateAdditionalPrefix
	case additionalToolsArray:
		return openAIWebSearchAppendAdditional
	default:
		return openAIWebSearchCreateAdditional
	}
}

func isOpenAIWebSearchToolType(toolType string) bool {
	return strings.HasPrefix(strings.TrimSpace(toolType), "web_search")
}

func openAIToolsContainWebSearch(rawTools any) bool {
	tools, ok := rawTools.([]any)
	if !ok {
		return false
	}
	for _, rawTool := range tools {
		tool, ok := rawTool.(map[string]any)
		if ok && isOpenAIWebSearchToolType(firstNonEmptyString(tool["type"])) {
			return true
		}
	}
	return false
}

// shouldPinOpenAIWebSearchHistoryToolChoice reports whether tool_choice may be
// forced to "none": only when the caller offered no tools, so the choice
// cannot meaningfully be anything else.
func shouldPinOpenAIWebSearchHistoryToolChoice(choice any) bool {
	switch typed := choice.(type) {
	case nil:
		return true
	case string:
		normalized := strings.ToLower(strings.TrimSpace(typed))
		return normalized == "" || normalized == "auto" || normalized == "none"
	default:
		return false
	}
}

// ensureOpenAIOAuthWebSearchToolForHistory is the map variant used by the
// Codex OAuth transform.
func ensureOpenAIOAuthWebSearchToolForHistory(reqBody map[string]any, responsesLite bool) bool {
	if reqBody == nil {
		return false
	}
	input, ok := reqBody["input"].([]any)
	if !ok {
		return false
	}
	scan := newOpenAIWebSearchHistoryScan()
	for i, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok {
			continue
		}
		tools, _ := item["tools"].([]any)
		if !scan.observeItem(
			i,
			firstNonEmptyString(item["type"]),
			responsesLite,
			len(tools),
			openAIToolsContainWebSearch(item["tools"]),
		) {
			return false
		}
	}
	if !scan.hasWebSearchCall || openAIToolsContainWebSearch(reqBody["tools"]) {
		return false
	}
	tools, _ := reqBody["tools"].([]any)
	scan.callerDeclaredTools = scan.callerDeclaredTools || len(tools) > 0
	additionalToolsArray := false
	if scan.additionalToolsAt >= 0 {
		item, _ := input[scan.additionalToolsAt].(map[string]any)
		_, additionalToolsArray = item["tools"].([]any)
	}
	switch scan.injectionPlan(responsesLite, len(tools) > 0, additionalToolsArray) {
	case openAIWebSearchAppendTopLevel:
		reqBody["tools"] = append(tools, cloneOpenAIWebSearchHistoryTool())
	case openAIWebSearchCreateTopLevel:
		reqBody["tools"] = []any{cloneOpenAIWebSearchHistoryTool()}
	case openAIWebSearchAppendAdditional:
		// additionalToolsIndex is only recorded for map items.
		item, _ := input[scan.additionalToolsAt].(map[string]any)
		existing, _ := item["tools"].([]any)
		item["tools"] = append(existing, cloneOpenAIWebSearchHistoryTool())
	case openAIWebSearchCreateAdditional:
		item, _ := input[scan.additionalToolsAt].(map[string]any)
		item["tools"] = []any{cloneOpenAIWebSearchHistoryTool()}
	case openAIWebSearchCreateAdditionalPrefix:
		// Lite declarations belong in the input prefix. A trailing compaction
		// trigger remains last because the new item is inserted at index zero.
		at := 0
		additional := map[string]any{
			"type":  openAIAdditionalToolsItemType,
			"role":  openAIAdditionalToolsDefaultRole,
			"tools": []any{cloneOpenAIWebSearchHistoryTool()},
		}
		next := make([]any, 0, len(input)+1)
		next = append(next, input[:at]...)
		next = append(next, additional)
		next = append(next, input[at:]...)
		reqBody["input"] = next
	}
	if !scan.callerDeclaredTools {
		if choice, exists := reqBody["tool_choice"]; !exists || shouldPinOpenAIWebSearchHistoryToolChoice(choice) {
			reqBody["tool_choice"] = "none"
		}
	}
	return true
}

// ensureOpenAIOAuthWebSearchToolForHistoryBody is the raw-body variant used by
// the passthrough and WebSocket paths; it avoids decoding the whole body.
func ensureOpenAIOAuthWebSearchToolForHistoryBody(body []byte, responsesLite bool) ([]byte, bool, error) {
	if len(body) == 0 || !bytes.Contains(body, []byte(openAIWebSearchCallItemType)) {
		return body, false, nil
	}
	input := gjson.GetBytes(body, "input")
	if !input.IsArray() {
		return body, false, nil
	}
	items := input.Array()
	scan := newOpenAIWebSearchHistoryScan()
	for i, item := range items {
		itemTools := item.Get("tools")
		itemToolCount := 0
		if itemTools.IsArray() {
			itemToolCount = len(itemTools.Array())
		}
		if !scan.observeItem(
			i,
			item.Get("type").String(),
			responsesLite,
			itemToolCount,
			gjsonToolsContainWebSearch(itemTools),
		) {
			return body, false, nil
		}
	}
	if !scan.hasWebSearchCall {
		return body, false, nil
	}
	tools := gjson.GetBytes(body, "tools")
	if gjsonToolsContainWebSearch(tools) {
		return body, false, nil
	}
	topLevelTools := tools.IsArray() && len(tools.Array()) > 0
	scan.callerDeclaredTools = scan.callerDeclaredTools || topLevelTools
	additionalToolsArray := scan.additionalToolsAt >= 0 &&
		items[scan.additionalToolsAt].Get("tools").IsArray()

	var (
		next []byte
		err  error
	)
	switch scan.injectionPlan(responsesLite, topLevelTools, additionalToolsArray) {
	case openAIWebSearchAppendTopLevel:
		next, err = sjson.SetBytes(body, "tools.-1", cloneOpenAIWebSearchHistoryTool())
	case openAIWebSearchCreateTopLevel:
		next, err = sjson.SetBytes(body, "tools", []any{cloneOpenAIWebSearchHistoryTool()})
	case openAIWebSearchAppendAdditional:
		next, err = sjson.SetBytes(body, fmt.Sprintf("input.%d.tools.-1", scan.additionalToolsAt), cloneOpenAIWebSearchHistoryTool())
	case openAIWebSearchCreateAdditional:
		next, err = sjson.SetBytes(body, fmt.Sprintf("input.%d.tools", scan.additionalToolsAt), []any{cloneOpenAIWebSearchHistoryTool()})
	case openAIWebSearchCreateAdditionalPrefix:
		// Lite declarations belong in the input prefix.
		next, err = insertOpenAIAdditionalToolsItemRaw(body, items, 0)
	}
	if err != nil {
		return body, false, fmt.Errorf("declare web_search tool for web_search_call history: %w", err)
	}
	if !scan.callerDeclaredTools {
		choice := gjson.GetBytes(next, "tool_choice")
		if !choice.Exists() || choice.Type == gjson.Null || (choice.Type == gjson.String && shouldPinOpenAIWebSearchHistoryToolChoice(choice.String())) {
			next, err = sjson.SetBytes(next, "tool_choice", "none")
			if err != nil {
				return body, false, fmt.Errorf("pin tool_choice for web_search_call history: %w", err)
			}
		}
	}
	return next, true, nil
}

func insertOpenAIAdditionalToolsItemRaw(body []byte, items []gjson.Result, at int) ([]byte, error) {
	additional, err := marshalOpenAIUpstreamJSON(map[string]any{
		"type":  openAIAdditionalToolsItemType,
		"role":  openAIAdditionalToolsDefaultRole,
		"tools": []any{cloneOpenAIWebSearchHistoryTool()},
	})
	if err != nil {
		return nil, err
	}
	rawItems := make([][]byte, 0, len(items)+1)
	for i, item := range items {
		if i == at {
			rawItems = append(rawItems, additional)
		}
		rawItems = append(rawItems, []byte(item.Raw))
	}
	if at >= len(items) {
		rawItems = append(rawItems, additional)
	}
	raw := append([]byte{'['}, bytes.Join(rawItems, []byte{','})...)
	raw = append(raw, ']')
	return sjson.SetRawBytes(body, "input", raw)
}

func gjsonToolsContainWebSearch(tools gjson.Result) bool {
	if !tools.IsArray() {
		return false
	}
	for _, tool := range tools.Array() {
		if isOpenAIWebSearchToolType(tool.Get("type").String()) {
			return true
		}
	}
	return false
}

func cloneOpenAIWebSearchHistoryTool() map[string]any {
	tool := make(map[string]any, len(openAIWebSearchHistoryTool))
	for key, value := range openAIWebSearchHistoryTool {
		tool[key] = value
	}
	return tool
}
