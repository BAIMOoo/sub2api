package service

import (
	"fmt"
	"reflect"
	"strings"
)

// canonicalizeCodexExecLiteRequest makes an already-normalized Responses Lite
// request use the layout emitted by codex exec 0.171.0: base instructions are
// developer messages, declared tools live in one input-prefix carrier, and
// requests with no declared tools omit an empty additional_tools carrier.
func canonicalizeCodexExecLiteRequest(reqBody map[string]any) (bool, error) {
	if reqBody == nil {
		return false, nil
	}
	toolsChanged, err := canonicalizeCodexExecLiteTools(reqBody)
	if err != nil {
		return false, err
	}
	instructionsChanged, err := promoteCodexInstructionsToDeveloperInput(reqBody)
	if err != nil {
		return false, err
	}
	return instructionsChanged || toolsChanged, nil
}

func canonicalizeCodexExecLiteTools(reqBody map[string]any) (bool, error) {
	var topLevelTools []any
	rawTools, hasTopLevelTools := reqBody["tools"]
	if hasTopLevelTools && rawTools != nil {
		var ok bool
		topLevelTools, ok = rawTools.([]any)
		if !ok {
			return false, fmt.Errorf("codex exec Lite tools must be an array")
		}
	}

	var input []any
	if rawInput, hasInput := reqBody["input"]; hasInput && rawInput != nil {
		var ok bool
		input, ok = rawInput.([]any)
		if !ok {
			return false, newOpenAIResponsesLiteValidationError("input", "codex exec Lite input must be an array")
		}
	} else if !hasTopLevelTools {
		return false, nil
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
		} else if _, hasID := item["id"]; hasID {
			return false, newOpenAIResponsesLiteValidationError("input", "additional_tools carrier id cannot be preserved when merging Lite carriers")
		}
		carrierCount++
		existingTools = append(existingTools, itemTools...)
	}

	mergedTools, toolsChanged, err := canonicalizeCodexExecLiteAdditionalTools(existingTools, topLevelTools)
	if err != nil {
		return false, err
	}
	if len(mergedTools) == 0 {
		if carrier == nil {
			if !hasTopLevelTools {
				return false, nil
			}
			delete(reqBody, "tools")
			return true, nil
		}
		if _, hasID := carrier["id"]; !hasID {
			delete(reqBody, "tools")
			reqBody["input"] = filteredInput
			return true, nil
		}
	}
	if !hasTopLevelTools &&
		(carrier == nil || carrierIndex == 0 && carrierCount == 1 && !toolsChanged) {
		return false, nil
	}
	delete(reqBody, "tools")
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

func canonicalizeCodexExecLiteAdditionalTools(existing []any, moved []any) ([]any, bool, error) {
	changed := len(moved) > 0
	allTools := make([]any, 0, len(existing)+len(moved))
	allTools = append(allTools, existing...)
	allTools = append(allTools, moved...)

	directTools := make([]any, 0, len(allTools))
	namespaceTools := make([]any, 0)
	var functionsNamespace map[string]any
	functionsNamespaceIndex := -1
	functionsNamespaceCount := 0
	for _, rawTool := range allTools {
		tool, ok, err := canonicalizeCodexExecLiteNamespaceTool(rawTool)
		if err != nil {
			return nil, false, err
		}
		if ok {
			changed = true
			if functionsNamespaceIndex < 0 {
				functionsNamespaceIndex = len(directTools)
			}
			namespaceTools = append(namespaceTools, tool)
			continue
		}
		if tool, ok := rawTool.(map[string]any); ok && isCodexFunctionsNamespace(tool) {
			functionsNamespaceCount++
			if functionsNamespaceCount > 1 {
				changed = true
			}
			if _, hasChildren := tool["children"]; hasChildren {
				changed = true
			}
			if functionsNamespaceIndex < 0 {
				functionsNamespaceIndex = len(directTools)
			}
			if functionsNamespace == nil {
				functionsNamespace = tool
			}
			toolList, err := codexExecLiteFunctionsNamespaceTools(tool)
			if err != nil {
				return nil, false, err
			}
			for _, child := range toolList {
				normalized, ok, err := canonicalizeCodexExecLiteNamespaceTool(child)
				if err != nil {
					return nil, false, err
				}
				if ok {
					if !reflect.DeepEqual(child, normalized) {
						changed = true
					}
					child = normalized
				}
				namespaceTools = append(namespaceTools, child)
			}
			continue
		}
		directTools = append(directTools, rawTool)
	}

	if !changed {
		if _, err := mergeOpenAIResponsesLiteAdditionalTools(nil, namespaceTools); err != nil {
			return nil, false, err
		}
		if _, err := mergeOpenAIResponsesLiteAdditionalTools(nil, directTools); err != nil {
			return nil, false, err
		}
		return allTools, false, nil
	}
	if functionsNamespaceIndex >= 0 {
		if functionsNamespace == nil {
			functionsNamespace = map[string]any{
				"type":        "namespace",
				"name":        codexAdditionalToolsNamespaceName,
				"description": codexAdditionalToolsNamespaceDescription,
			}
		}
		mergedNamespaceTools, err := mergeOpenAIResponsesLiteAdditionalTools(nil, namespaceTools)
		if err != nil {
			return nil, false, err
		}
		if mergedNamespaceTools == nil {
			mergedNamespaceTools = []any{}
		}
		functionsNamespace["tools"] = mergedNamespaceTools
		delete(functionsNamespace, "children")
		next := make([]any, 0, len(directTools)+1)
		next = append(next, directTools[:functionsNamespaceIndex]...)
		next = append(next, functionsNamespace)
		next = append(next, directTools[functionsNamespaceIndex:]...)
		directTools = next
	}
	mergedDirectTools, err := mergeOpenAIResponsesLiteAdditionalTools(nil, directTools)
	if err != nil {
		return nil, false, err
	}
	return mergedDirectTools, true, nil
}

func codexExecLiteFunctionsNamespaceTools(namespace map[string]any) ([]any, error) {
	rawTools, hasTools := namespace["tools"]
	rawChildren, hasChildren := namespace["children"]
	if hasTools && rawTools != nil && hasChildren && rawChildren != nil {
		return nil, fmt.Errorf("codex exec Lite functions namespace must use exactly one of tools or children")
	}
	rawList := rawTools
	if hasChildren && rawChildren != nil {
		rawList = rawChildren
	}
	if rawList == nil {
		return nil, nil
	}
	toolList, ok := rawList.([]any)
	if !ok {
		return nil, fmt.Errorf("codex exec Lite functions namespace tools must be an array")
	}
	return toolList, nil
}

func isCodexFunctionsNamespace(tool map[string]any) bool {
	return strings.TrimSpace(firstNonEmptyString(tool["type"])) == "namespace" &&
		strings.TrimSpace(firstNonEmptyString(tool["name"])) == codexAdditionalToolsNamespaceName
}

func canonicalizeCodexExecLiteNamespaceTool(rawTool any) (any, bool, error) {
	if customTool, ok := rawTool.(string); ok {
		name := strings.TrimSpace(customTool)
		if name == "" {
			return nil, false, fmt.Errorf("codex exec Lite custom tool must not be empty")
		}
		return map[string]any{"type": "custom", "name": name}, true, nil
	}
	tool, ok := rawTool.(map[string]any)
	if !ok {
		return nil, false, nil
	}
	if classifyOpenAIResponsesLiteTool(tool) != openAIResponsesLiteToolWorkspace {
		return nil, false, nil
	}
	toolType := strings.TrimSpace(firstNonEmptyString(tool["type"]))
	switch toolType {
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
	}
	if toolType != "tool_search" && strings.TrimSpace(firstNonEmptyString(tool["name"])) == "" {
		return nil, false, nil
	}
	if toolType == "function" && strings.TrimSpace(firstNonEmptyString(tool["description"])) == "" {
		return nil, false, nil
	}
	return tool, true, nil
}

func promoteCodexInstructionsToDeveloperInput(reqBody map[string]any) (bool, error) {
	rawInstructions, exists := reqBody["instructions"]
	if !exists {
		return false, nil
	}
	if rawInstructions == nil {
		delete(reqBody, "instructions")
		return true, nil
	}
	instructions, ok := rawInstructions.(string)
	if !ok {
		return false, newOpenAIResponsesLiteValidationError(
			"instructions",
			"instructions must be a string for lossless Codex conversion",
		)
	}
	if strings.TrimSpace(instructions) == "" {
		delete(reqBody, "instructions")
		return true, nil
	}
	var input []any
	if rawInput, hasInput := reqBody["input"]; hasInput && rawInput != nil {
		var inputOK bool
		input, inputOK = rawInput.([]any)
		if !inputOK {
			return false, newOpenAIResponsesLiteValidationError("input", "codex input must be an array")
		}
	}

	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok || !isCodexExecLiteBaseInstructionsMessage(item) {
			continue
		}
		if !ensureCodexMessageInputText(item, instructions) {
			return false, newOpenAIResponsesLiteValidationError(
				"instructions",
				"instructions cannot be merged into existing base instructions message",
			)
		}
		delete(reqBody, "instructions")
		return true, nil
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
	return true, nil
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

func ensureCodexMessageInputText(item map[string]any, text string) bool {
	switch content := item["content"].(type) {
	case string:
		if content == text {
			return true
		}
		item["content"] = []any{
			map[string]any{"type": "input_text", "text": content},
			map[string]any{"type": "input_text", "text": text},
		}
	case []any:
		for _, rawPart := range content {
			part, ok := rawPart.(map[string]any)
			if ok && strings.TrimSpace(firstNonEmptyString(part["type"])) == "input_text" &&
				firstNonEmptyString(part["text"]) == text {
				return true
			}
		}
		item["content"] = append(content, map[string]any{"type": "input_text", "text": text})
	case nil:
		item["content"] = []any{map[string]any{"type": "input_text", "text": text}}
	default:
		return false
	}
	metadata, _ := item[openAIOAuthInputMetadataField].(map[string]any)
	if metadata != nil {
		kinds, _ := metadata["content_item_kinds"].([]any)
		metadata["content_item_kinds"] = append(kinds, "model.base_instructions")
	}
	return true
}
