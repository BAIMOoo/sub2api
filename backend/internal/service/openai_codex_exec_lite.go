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

	if !hasTopLevelTools && carrier != nil && carrierIndex == 0 && carrierCount == 1 &&
		!codexExecLiteAdditionalToolsNeedCanonicalization(existingTools) {
		return false, nil
	}
	mergedTools, err := canonicalizeCodexExecLiteAdditionalTools(existingTools, topLevelTools)
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

func canonicalizeCodexExecLiteAdditionalTools(existing []any, moved []any) ([]any, error) {
	allTools := make([]any, 0, len(existing)+len(moved))
	allTools = append(allTools, existing...)
	allTools = append(allTools, moved...)

	directTools := make([]any, 0, len(allTools))
	namespaceTools := make([]any, 0)
	var functionsNamespace map[string]any
	functionsNamespaceAt := -1
	for _, rawTool := range allTools {
		if tool, ok := canonicalizeCodexExecLiteNamespaceTool(rawTool); ok {
			if functionsNamespaceAt < 0 {
				functionsNamespaceAt = len(directTools)
			}
			namespaceTools = append(namespaceTools, tool)
			continue
		}
		if tool, ok := rawTool.(map[string]any); ok && isCodexFunctionsNamespace(tool) {
			if functionsNamespaceAt < 0 {
				functionsNamespaceAt = len(directTools)
			}
			if functionsNamespace == nil {
				functionsNamespace = tool
			}
			if rawNamespaceTools, exists := tool["tools"]; exists && rawNamespaceTools != nil {
				toolList, ok := rawNamespaceTools.([]any)
				if !ok {
					return nil, fmt.Errorf("codex exec Lite functions namespace tools must be an array")
				}
				namespaceTools = append(namespaceTools, toolList...)
			}
			continue
		}
		directTools = append(directTools, rawTool)
	}

	if functionsNamespaceAt >= 0 {
		if functionsNamespace == nil {
			functionsNamespace = map[string]any{
				"type":        "namespace",
				"name":        codexAdditionalToolsNamespaceName,
				"description": codexAdditionalToolsNamespaceDescription,
			}
		}
		mergedNamespaceTools, err := mergeOpenAIResponsesLiteAdditionalTools(nil, namespaceTools)
		if err != nil {
			return nil, err
		}
		functionsNamespace["tools"] = mergedNamespaceTools
		next := make([]any, 0, len(directTools)+1)
		next = append(next, directTools[:functionsNamespaceAt]...)
		next = append(next, functionsNamespace)
		next = append(next, directTools[functionsNamespaceAt:]...)
		directTools = next
	}
	return mergeOpenAIResponsesLiteAdditionalTools(nil, directTools)
}

func codexExecLiteAdditionalToolsNeedCanonicalization(tools []any) bool {
	for _, rawTool := range tools {
		if _, ok := canonicalizeCodexExecLiteNamespaceTool(rawTool); ok {
			return true
		}
	}
	return false
}

func isCodexFunctionsNamespace(tool map[string]any) bool {
	return strings.TrimSpace(firstNonEmptyString(tool["type"])) == "namespace" &&
		strings.TrimSpace(firstNonEmptyString(tool["name"])) == codexAdditionalToolsNamespaceName
}

func canonicalizeCodexExecLiteNamespaceTool(rawTool any) (any, bool) {
	tool, ok := rawTool.(map[string]any)
	if !ok {
		return nil, false
	}
	switch strings.TrimSpace(firstNonEmptyString(tool["type"])) {
	case "function":
		nested, hasNested := tool["function"].(map[string]any)
		if hasNested {
			flattened := make(map[string]any, len(tool)+len(nested))
			for key, value := range tool {
				if key == "function" {
					continue
				}
				flattened[key] = value
			}
			for key, value := range nested {
				flattened[key] = value
			}
			flattened["type"] = "function"
			tool = flattened
		}
	case "custom", "tool_search":
		return tool, true
	default:
		return nil, false
	}
	if strings.TrimSpace(firstNonEmptyString(tool["name"])) == "" ||
		strings.TrimSpace(firstNonEmptyString(tool["description"])) == "" {
		return nil, false
	}
	return tool, true
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

	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok || !isCodexExecLiteBaseInstructionsMessage(item) {
			continue
		}
		if !codexMessageContainsInputText(item, instructions) &&
			!appendCodexMessageInputText(item, instructions) {
			return false
		}
		delete(reqBody, "instructions")
		return true
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

func isCodexExecLiteBaseInstructionsMessage(item map[string]any) bool {
	if strings.TrimSpace(firstNonEmptyString(item["type"])) != "message" ||
		strings.TrimSpace(firstNonEmptyString(item["role"])) != "developer" {
		return false
	}
	metadata, _ := item[openAIOAuthInputMetadataField].(map[string]any)
	rawKinds, _ := metadata["content_item_kinds"].([]any)
	for _, rawKind := range rawKinds {
		if strings.TrimSpace(firstNonEmptyString(rawKind)) == "model.base_instructions" {
			return true
		}
	}
	return false
}

func codexMessageContainsInputText(item map[string]any, text string) bool {
	switch content := item["content"].(type) {
	case string:
		return content == text
	case []any:
		for _, rawPart := range content {
			part, ok := rawPart.(map[string]any)
			if ok && strings.TrimSpace(firstNonEmptyString(part["type"])) == "input_text" &&
				firstNonEmptyString(part["text"]) == text {
				return true
			}
		}
	}
	return false
}

func appendCodexMessageInputText(item map[string]any, text string) bool {
	part := map[string]any{"type": "input_text", "text": text}
	switch content := item["content"].(type) {
	case nil:
		item["content"] = []any{part}
	case string:
		if content == text {
			return true
		}
		item["content"] = []any{
			map[string]any{"type": "input_text", "text": content},
			part,
		}
	case []any:
		item["content"] = append(content, part)
	default:
		return false
	}
	appendCodexBaseInstructionContentKind(item)
	return true
}

func appendCodexBaseInstructionContentKind(item map[string]any) {
	metadata, _ := item[openAIOAuthInputMetadataField].(map[string]any)
	if metadata == nil {
		return
	}
	kinds, _ := metadata["content_item_kinds"].([]any)
	metadata["content_item_kinds"] = append(kinds, "model.base_instructions")
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
