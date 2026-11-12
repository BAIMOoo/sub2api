package service

import (
	"bytes"
	"fmt"
	"strings"
)

// canonicalizeCodexExecLiteRequest makes an already-normalized Responses Lite
// request use the layout emitted by codex exec: base instructions are developer
// messages, and every tool declaration lives in one input-prefix carrier.
func canonicalizeCodexExecLiteRequest(reqBody map[string]any) (bool, error) {
	if reqBody == nil {
		return false, nil
	}
	toolsChanged, err := canonicalizeCodexExecLiteTools(reqBody)
	if err != nil {
		return false, err
	}
	return promoteCodexInstructionsToDeveloperInput(reqBody) || toolsChanged, nil
}

func canonicalizeCodexExecLiteTools(reqBody map[string]any) (bool, error) {
	input, ok := reqBody["input"].([]any)
	if !ok {
		return false, nil
	}

	var topLevelTools []any
	rawTools, hasTopLevelTools := reqBody["tools"]
	if hasTopLevelTools && rawTools != nil {
		var ok bool
		topLevelTools, ok = rawTools.([]any)
		if !ok {
			return false, fmt.Errorf("codex exec Lite tools must be an array")
		}
	}

	var carrier map[string]any
	carrierIndex := -1
	carrierCount := 0
	existingTools := make([]any, 0)
	filteredInput := make([]any, 0, len(input))
	for index, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok || strings.TrimSpace(firstNonEmptyString(item["type"])) != openAIAdditionalToolsItemType {
			filteredInput = append(filteredInput, rawItem)
			continue
		}
		itemTools := []any(nil)
		if rawItemTools, exists := item["tools"]; exists && rawItemTools != nil {
			var toolsOK bool
			itemTools, toolsOK = rawItemTools.([]any)
			if !toolsOK {
				return false, fmt.Errorf("codex exec Lite additional_tools.tools must be an array")
			}
		}
		if carrier == nil {
			carrier = item
			carrierIndex = index
		}
		carrierCount++
		existingTools = append(existingTools, itemTools...)
	}

	if !hasTopLevelTools && (carrier == nil || carrierIndex == 0 && carrierCount == 1) {
		return false, nil
	}
	mergedTools, err := mergeOpenAIResponsesLiteAdditionalTools(existingTools, topLevelTools)
	if err != nil {
		return false, err
	}
	delete(reqBody, "tools")
	if carrier == nil && len(topLevelTools) == 0 {
		return true, nil
	}
	if carrier == nil {
		carrier = map[string]any{
			"type": openAIAdditionalToolsItemType,
			"role": openAIAdditionalToolsDefaultRole,
		}
	}
	carrier["tools"] = mergedTools
	reqBody["input"] = append([]any{carrier}, filteredInput...)
	return true, nil
}

func promoteCodexInstructionsToDeveloperInput(reqBody map[string]any) bool {
	rawInstructions, exists := reqBody["instructions"]
	if !exists {
		return false
	}
	instructions, ok := rawInstructions.(string)
	if !ok || strings.TrimSpace(instructions) == "" {
		return false
	}
	input, ok := reqBody["input"].([]any)
	if !ok {
		return false
	}

	insertAt := 0
	for insertAt < len(input) {
		item, ok := input[insertAt].(map[string]any)
		if !ok || strings.TrimSpace(firstNonEmptyString(item["type"])) != openAIAdditionalToolsItemType {
			break
		}
		insertAt++
	}
	message := map[string]any{
		"type": "message",
		"role": "developer",
		"content": []any{
			map[string]any{"type": "input_text", "text": instructions},
		},
		"internal_chat_message_metadata_passthrough": map[string]any{
			"content_item_kinds": []any{"model.base_instructions"},
		},
	}
	next := make([]any, 0, len(input)+1)
	next = append(next, input[:insertAt]...)
	next = append(next, message)
	next = append(next, input[insertAt:]...)
	reqBody["input"] = next
	delete(reqBody, "instructions")
	return true
}

func canonicalizeCodexExecLiteBody(body []byte) ([]byte, bool, error) {
	if !bytes.Contains(body, []byte(`"instructions"`)) &&
		!bytes.Contains(body, []byte(`"tools"`)) &&
		!bytes.Contains(body, []byte(`"additional_tools"`)) {
		return body, false, nil
	}
	var reqBody map[string]any
	if err := decodeOpenAIJSONUseNumber(body, &reqBody); err != nil {
		return body, false, fmt.Errorf("decode codex exec Lite body: %w", err)
	}
	changed, err := canonicalizeCodexExecLiteRequest(reqBody)
	if err != nil || !changed {
		return body, false, err
	}
	rebuilt, err := marshalOpenAIUpstreamJSON(reqBody)
	if err != nil {
		return body, false, fmt.Errorf("encode codex exec Lite body: %w", err)
	}
	return rebuilt, true, nil
}
