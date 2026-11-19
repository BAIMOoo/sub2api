package apicompat

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
)

type chatMessageContent struct {
	Text  *string
	Parts []ChatContentPart
}

type ChatCompletionsConversionMode uint8

const (
	ChatCompletionsConversionCompatible ChatCompletionsConversionMode = iota
	ChatCompletionsConversionFixedCodexHTTP
)

type ChatCompletionsToResponsesOptions struct {
	Mode ChatCompletionsConversionMode
}

// ChatCompletionsToResponses converts a Chat Completions request into a
// Responses API request. The upstream always streams, so Stream is forced to
// true. store is always false and reasoning.encrypted_content is always
// included so that the response translator has full context.
func ChatCompletionsToResponses(req *ChatCompletionsRequest) (*ResponsesRequest, error) {
	return ChatCompletionsToResponsesWithMinOutputTokens(req, DefaultMinMaxOutputTokens)
}

// ChatCompletionsToResponsesWithMinOutputTokens converts a Chat Completions
// request using the supplied max_output_tokens floor.
func ChatCompletionsToResponsesWithMinOutputTokens(req *ChatCompletionsRequest, minOutputTokens int) (*ResponsesRequest, error) {
	return ChatCompletionsToResponsesWithOptions(
		req,
		minOutputTokens,
		ChatCompletionsToResponsesOptions{},
	)
}

func ChatCompletionsToResponsesWithOptions(
	req *ChatCompletionsRequest,
	minOutputTokens int,
	opts ChatCompletionsToResponsesOptions,
) (*ResponsesRequest, error) {
	if err := openai.ValidateGPT61ReasoningEffort(req.Model, req.ReasoningEffort); err != nil {
		return nil, err
	}
	input, err := convertChatMessagesToResponsesInputWithMode(req.Messages, opts.Mode)
	if err != nil {
		return nil, err
	}

	inputJSON, err := json.Marshal(input)
	if err != nil {
		return nil, err
	}

	out := &ResponsesRequest{
		Model:              req.Model,
		Instructions:       req.Instructions,
		Input:              inputJSON,
		Stream:             true, // upstream always streams
		Include:            []string{"reasoning.encrypted_content"},
		ServiceTier:        req.ServiceTier,
		PromptCacheOptions: req.PromptCacheOptions,
		ParallelToolCalls:  req.ParallelToolCalls,
	}

	// Reasoning models (gpt-5.x) do not accept sampling parameters.
	// See isReasoningModel in anthropic_to_responses.go.
	if !isReasoningModel(req.Model) || (openai.IsGPT6SolOrLunaModelSpelling(req.Model) && req.ReasoningEffort == "none") {
		out.Temperature = req.Temperature
		out.TopP = req.TopP
	}

	storeFalse := false
	out.Store = &storeFalse

	// max_tokens / max_completion_tokens → max_output_tokens, prefer max_completion_tokens
	maxTokens := 0
	if req.MaxTokens != nil {
		maxTokens = *req.MaxTokens
	}
	if req.MaxCompletionTokens != nil {
		maxTokens = *req.MaxCompletionTokens
	}
	if maxTokens > 0 {
		v := maxTokens
		if v < minOutputTokens {
			v = minOutputTokens
		}
		out.MaxOutputTokens = &v
	}

	// reasoning_effort → reasoning.effort + reasoning.summary="auto"
	if req.ReasoningEffort != "" {
		out.Reasoning = &ResponsesReasoning{
			Effort:  req.ReasoningEffort,
			Summary: "auto",
		}
	}

	if format := chatResponseFormatToResponsesTextFormat(req.ResponseFormat); len(format) > 0 {
		if out.Text == nil {
			out.Text = &ResponsesText{}
		}
		out.Text.Format = format
	}

	// tools[] and legacy functions[] → ResponsesTool[]
	out.Tools, err = convertChatToolsToResponsesWithMode(req.Tools, req.Functions, opts.Mode)
	if err != nil {
		return nil, err
	}

	// tool_choice: strings and Responses-shaped objects pass through; a named
	// Chat choice nests the name under "function" and must be flattened.
	// Legacy function_call needs mapping.
	out.ToolChoice, err = convertChatToolChoiceToResponsesWithMode(
		req.ToolChoice,
		req.FunctionCall,
		out.Tools,
		opts.Mode,
	)
	if err != nil {
		return nil, err
	}

	return out, nil
}

func convertChatMessagesToResponsesInputWithMode(
	msgs []ChatMessage,
	mode ChatCompletionsConversionMode,
) ([]map[string]any, error) {
	out := make([]map[string]any, 0, len(msgs))
	legacyIDs := legacyFunctionCallIDs{msgs: msgs}
	callNames := make(map[string]string)
	legacyFunctionNames := make(map[string]bool)
	for index, message := range msgs {
		if message.Role == "assistant" && message.FunctionCall != nil {
			if mode == ChatCompletionsConversionFixedCodexHTTP && len(message.ToolCalls) > 0 {
				return nil, fmt.Errorf("messages[%d]: cannot preserve both function_call and tool_calls", index)
			}
			if mode == ChatCompletionsConversionFixedCodexHTTP &&
				legacyFunctionNames[message.FunctionCall.Name] {
				return nil, fmt.Errorf(
					"messages[%d]: duplicate legacy function name %q cannot be paired losslessly",
					index,
					message.FunctionCall.Name,
				)
			}
			legacyFunctionNames[message.FunctionCall.Name] = true
			if len(message.ToolCalls) == 0 {
				message.ToolCalls = []ChatToolCall{{
					ID:       legacyIDs.assign(message.FunctionCall.Name),
					Type:     "function",
					Function: *message.FunctionCall,
				}}
				message.FunctionCall = nil
			}
		}
		if message.Role == "function" && message.ToolCallID == "" {
			message.ToolCallID = legacyIDs.claim(message.Name)
		}
		items, err := convertChatMessageToResponsesItemsWithMode(message, mode)
		if err != nil {
			return nil, fmt.Errorf("messages[%d]: %w", index, err)
		}
		for _, call := range message.ToolCalls {
			if mode == ChatCompletionsConversionFixedCodexHTTP {
				if _, exists := callNames[call.ID]; exists {
					return nil, fmt.Errorf("messages[%d]: duplicate tool call id cannot be preserved", index)
				}
			}
			callNames[call.ID] = call.Function.Name
		}
		if mode == ChatCompletionsConversionFixedCodexHTTP && message.Role == "function" {
			name, exists := callNames[message.ToolCallID]
			if !exists || name != message.Name {
				return nil, fmt.Errorf("messages[%d]: function result has no matching named call", index)
			}
		}
		if mode == ChatCompletionsConversionFixedCodexHTTP && message.Role == "tool" {
			name, exists := callNames[message.ToolCallID]
			if !exists || name == "" {
				return nil, fmt.Errorf("messages[%d]: tool result has no matching named call", index)
			}
		}
		out = append(out, items...)
	}
	return out, nil
}

func convertChatMessageToResponsesItemsWithMode(
	message ChatMessage,
	mode ChatCompletionsConversionMode,
) ([]map[string]any, error) {
	strict := mode == ChatCompletionsConversionFixedCodexHTTP
	switch message.Role {
	case "system", "developer", "user", "assistant", "tool", "function":
	default:
		if strict {
			return nil, fmt.Errorf("unsupported role %q", message.Role)
		}
		message.Role = "user"
	}
	if strict && (message.ReasoningContent != "" || message.Reasoning != "") {
		return nil, fmt.Errorf("reasoning text cannot be preserved as valid Codex reasoning replay")
	}
	if strict && message.Name != "" && message.Role != "function" {
		return nil, fmt.Errorf("message name cannot be preserved in Responses")
	}
	if strict && (message.FunctionCall != nil || (len(message.ToolCalls) > 0 && message.Role != "assistant")) {
		return nil, fmt.Errorf("function_call and tool_calls require an assistant message")
	}
	if strict && message.ToolCallID != "" && message.Role != "tool" && message.Role != "function" {
		return nil, fmt.Errorf("tool_call_id requires a tool or function result")
	}
	if strict && message.Refusal != "" && message.Role != "assistant" {
		return nil, fmt.Errorf("refusal requires an assistant message")
	}

	if message.Role == "tool" || message.Role == "function" {
		var output any
		if strict {
			if strings.TrimSpace(message.ToolCallID) == "" {
				return nil, fmt.Errorf("%s result requires a matching tool_call_id", message.Role)
			}
			preserved, err := preserveChatContent(message.Content, message.Role)
			if err != nil {
				return nil, err
			}
			output = preserved
		} else {
			text, err := parseChatContent(message.Content)
			if err != nil {
				return nil, err
			}
			if text == "" {
				text = "(empty)"
			}
			output = text
		}
		callID := message.ToolCallID
		if callID == "" {
			callID = message.Name
		}
		return []map[string]any{{
			"type":    "function_call_output",
			"call_id": callID,
			"output":  output,
		}}, nil
	}

	var content any
	if strict {
		preserved, err := preserveChatContent(message.Content, message.Role)
		if err != nil {
			return nil, err
		}
		content = preserved
	} else if message.Role == "assistant" {
		text := ""
		if message.ReasoningContent != "" {
			text = "<thinking>" + message.ReasoningContent + "</thinking>"
		}
		parsed, err := parseAssistantContent(message.Content)
		if err != nil {
			return nil, err
		}
		if parsed != "" {
			if text != "" {
				text += "\n"
			}
			text += parsed
		}
		content = []map[string]any{{"type": "output_text", "text": text}}
		if text == "" {
			content = []map[string]any{}
		}
	} else {
		parsed, err := parseChatMessageContent(message.Content)
		if err != nil {
			return nil, err
		}
		raw, err := marshalChatInputContent(parsed)
		if err != nil {
			return nil, err
		}
		if err := json.Unmarshal(raw, &content); err != nil {
			return nil, err
		}
	}

	if strict && message.Refusal != "" {
		var parts []map[string]any
		switch value := content.(type) {
		case string:
			parts = append(parts, map[string]any{"type": "output_text", "text": value})
		case []map[string]any:
			parts = value
		}
		content = append(parts, map[string]any{"type": "refusal", "refusal": message.Refusal})
	}

	var items []map[string]any
	emitMessage := message.Role != "assistant" ||
		(strict && (len(normalizedRawJSON(message.Content)) > 0 || message.Refusal != "" || len(message.ToolCalls) == 0)) ||
		(!strict && len(content.([]map[string]any)) > 0)
	if emitMessage {
		items = append(items, map[string]any{
			"type":    "message",
			"role":    message.Role,
			"content": content,
		})
	}
	for index, call := range message.ToolCalls {
		if strict && call.Type != "function" {
			return nil, fmt.Errorf("tool_calls[%d]: unsupported type %q", index, call.Type)
		}
		if strict && (strings.TrimSpace(call.ID) == "" || strings.TrimSpace(call.Function.Name) == "") {
			return nil, fmt.Errorf("tool_calls[%d]: function call requires an id and name", index)
		}
		if strict && call.Index != nil {
			return nil, fmt.Errorf("tool_calls[%d]: streaming index cannot be preserved in a request", index)
		}
		arguments := call.Function.Arguments
		if !strict && arguments == "" {
			arguments = "{}"
		}
		items = append(items, map[string]any{
			"type":      "function_call",
			"call_id":   call.ID,
			"name":      call.Function.Name,
			"arguments": arguments,
		})
	}
	return items, nil
}

func preserveChatContent(raw json.RawMessage, role string) (any, error) {
	raw = normalizedRawJSON(raw)
	if len(raw) == 0 {
		return "", nil
	}
	var text string
	if err := json.Unmarshal(raw, &text); err == nil {
		return text, nil
	}
	var rawParts []json.RawMessage
	if err := json.Unmarshal(raw, &rawParts); err != nil {
		return nil, fmt.Errorf("content must be a string or typed parts array")
	}
	parts := make([]map[string]any, 0, len(rawParts))
	for index, rawPart := range rawParts {
		part, err := preserveChatContentPart(rawPart, role)
		if err != nil {
			return nil, fmt.Errorf("content[%d]: %w", index, err)
		}
		parts = append(parts, part)
	}
	return parts, nil
}

func preserveChatContentPart(raw json.RawMessage, role string) (map[string]any, error) {
	var header struct {
		Type string `json:"type"`
	}
	if err := json.Unmarshal(raw, &header); err != nil {
		return nil, fmt.Errorf("content part must be a typed object")
	}
	var field string
	switch header.Type {
	case "text":
		field = "text"
	case "refusal":
		if role != "assistant" {
			return nil, fmt.Errorf("refusal content requires an assistant message")
		}
		field = "refusal"
	case "image_url", "file":
		if role != "user" && role != "system" && role != "developer" {
			return nil, fmt.Errorf("%s content cannot be preserved for role %s", header.Type, role)
		}
		field = header.Type
	default:
		return nil, fmt.Errorf("unsupported content type %q cannot be preserved", header.Type)
	}

	object, err := preservedChatObject(raw, "type", field, "prompt_cache_breakpoint")
	if err != nil {
		return nil, err
	}
	part := make(map[string]any)
	if cache, ok := object["prompt_cache_breakpoint"]; ok {
		part["prompt_cache_breakpoint"] = cache
	}
	switch header.Type {
	case "text", "refusal":
		text, err := preservedChatString(object[field])
		if err != nil {
			return nil, fmt.Errorf("%s: %w", field, err)
		}
		part["type"] = "input_text"
		if role == "assistant" {
			part["type"] = "output_text"
		}
		if header.Type == "refusal" {
			part["type"] = "refusal"
		}
		part[field] = text
	case "image_url":
		image, err := preservedChatObject(object[field], "url", "detail")
		if err != nil {
			return nil, fmt.Errorf("image_url: %w", err)
		}
		url, err := preservedChatString(image["url"])
		if err != nil || strings.TrimSpace(url) == "" || isEmptyBase64DataURI(url) {
			return nil, fmt.Errorf("image_url requires a nonempty image URL or data payload")
		}
		part["type"], part["image_url"] = "input_image", url
		if detail, ok := image["detail"]; ok {
			if _, err := preservedChatString(detail); err != nil {
				return nil, fmt.Errorf("image detail: %w", err)
			}
			part["detail"] = detail
		}
	case "file":
		file, err := preservedChatObject(object[field], "filename", "file_data", "file_id")
		if err != nil {
			return nil, fmt.Errorf("file: %w", err)
		}
		part["type"] = "input_file"
		hasPayload := false
		for key, value := range file {
			text, err := preservedChatString(value)
			if err != nil {
				return nil, fmt.Errorf("file.%s: %w", key, err)
			}
			part[key] = text
			if (key == "file_data" || key == "file_id") && strings.TrimSpace(text) != "" {
				hasPayload = true
			}
		}
		if !hasPayload {
			return nil, fmt.Errorf("file requires file_data or file_id")
		}
	}
	return part, nil
}

func preservedChatObject(raw json.RawMessage, fields ...string) (map[string]json.RawMessage, error) {
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil || object == nil {
		return nil, fmt.Errorf("expected an object")
	}
	for key := range object {
		allowed := false
		for _, field := range fields {
			if key == field {
				allowed = true
				break
			}
		}
		if !allowed {
			return nil, fmt.Errorf("unsupported field %q cannot be preserved", key)
		}
	}
	return object, nil
}

func preservedChatString(raw json.RawMessage) (string, error) {
	var value *string
	if err := json.Unmarshal(raw, &value); err != nil || value == nil {
		return "", fmt.Errorf("expected a string")
	}
	return *value, nil
}

func convertChatToolsToResponsesWithMode(
	tools []ChatTool,
	functions []ChatFunction,
	mode ChatCompletionsConversionMode,
) ([]ResponsesTool, error) {
	strict := mode == ChatCompletionsConversionFixedCodexHTTP
	var out []ResponsesTool
	names := make(map[string]bool)
	add := func(function ChatFunction) error {
		if strict {
			if strings.TrimSpace(function.Name) == "" {
				return fmt.Errorf("function requires a name")
			}
			if names[function.Name] {
				return fmt.Errorf("duplicate function name cannot be preserved")
			}
			if len(function.Parameters) > 0 {
				var schema map[string]json.RawMessage
				if err := json.Unmarshal(function.Parameters, &schema); err != nil || schema == nil {
					return fmt.Errorf("function parameters must be a JSON schema object")
				}
			}
			names[function.Name] = true
		}
		out = append(out, ResponsesTool{
			Type:        "function",
			Name:        function.Name,
			Description: function.Description,
			Parameters:  function.Parameters,
			Strict:      defaultStrictFalse(function.Strict),
		})
		return nil
	}
	for index, tool := range tools {
		toolType := strings.ToLower(strings.TrimSpace(tool.Type))
		switch toolType {
		case "web_search", "code_execution":
			out = append(out, ResponsesTool{Type: toolType})
		case "x_search":
			out = append(out, ResponsesTool{
				Type:                     "x_search",
				AllowedXHandles:          tool.AllowedXHandles,
				ExcludedXHandles:         tool.ExcludedXHandles,
				FromDate:                 tool.FromDate,
				ToDate:                   tool.ToDate,
				EnableImageUnderstanding: tool.EnableImageUnderstanding,
				EnableVideoUnderstanding: tool.EnableVideoUnderstanding,
			})
		case "function":
			if tool.Function == nil && strict {
				return nil, fmt.Errorf("tools[%d]: function definition is missing", index)
			}
			if strict && (len(tool.AllowedXHandles) > 0 ||
				len(tool.ExcludedXHandles) > 0 ||
				tool.FromDate != "" ||
				tool.ToDate != "" ||
				tool.EnableImageUnderstanding != nil ||
				tool.EnableVideoUnderstanding != nil) {
				return nil, fmt.Errorf("tools[%d]: search options cannot be preserved on a function tool", index)
			}
			if tool.Function == nil {
				continue
			}
			if err := add(*tool.Function); err != nil {
				return nil, fmt.Errorf("tools[%d]: %w", index, err)
			}
		default:
			if strict {
				return nil, fmt.Errorf(
					"tools[%d]: unsupported tool type %q or missing function definition",
					index,
					tool.Type,
				)
			}
		}
	}
	for index, function := range functions {
		if err := add(function); err != nil {
			return nil, fmt.Errorf("functions[%d]: %w", index, err)
		}
	}
	return out, nil
}

func convertChatToolChoiceToResponsesWithMode(
	choice, legacy json.RawMessage,
	tools []ResponsesTool,
	mode ChatCompletionsConversionMode,
) (json.RawMessage, error) {
	strict := mode == ChatCompletionsConversionFixedCodexHTTP
	if !strict {
		choice = normalizedRawJSON(choice)
		legacy = normalizedRawJSON(legacy)
		if len(choice) == 0 && len(legacy) == 0 {
			return nil, nil
		}
		if len(choice) > 0 {
			var object struct {
				Type     string `json:"type"`
				Function *struct {
					Name string `json:"name"`
				} `json:"function"`
			}
			if err := json.Unmarshal(choice, &object); err == nil &&
				object.Type == "function" &&
				object.Function != nil &&
				strings.TrimSpace(object.Function.Name) != "" {
				return json.Marshal(map[string]string{
					"type": "function",
					"name": strings.TrimSpace(object.Function.Name),
				})
			}
			return choice, nil
		}
		var text string
		if err := json.Unmarshal(legacy, &text); err == nil {
			return json.Marshal(text)
		}
		var object struct {
			Name string `json:"name"`
		}
		if err := json.Unmarshal(legacy, &object); err != nil {
			return nil, err
		}
		return json.Marshal(map[string]string{
			"type": "function",
			"name": object.Name,
		})
	}
	choice, legacy = normalizedRawJSON(choice), normalizedRawJSON(legacy)
	if len(choice) > 0 && len(legacy) > 0 {
		return nil, fmt.Errorf("cannot preserve both tool_choice and function_call policies")
	}
	field := "tool_choice"
	if len(legacy) > 0 {
		choice, field = legacy, "function_call"
	}
	if len(choice) == 0 {
		return nil, nil
	}
	if mode, err := preservedChatString(choice); err == nil {
		if mode != "auto" &&
			mode != "none" &&
			(mode != "required" || field == "function_call") {
			return nil, fmt.Errorf("%s: unsupported selection policy", field)
		}
		if mode == "required" && len(tools) == 0 {
			return nil, fmt.Errorf("tool_choice requires a declared tool")
		}
		return choice, nil
	}

	var nameRaw json.RawMessage
	if field == "function_call" {
		object, err := preservedChatObject(choice, "name")
		if err != nil {
			return nil, fmt.Errorf("function_call: %w", err)
		}
		nameRaw = object["name"]
	} else {
		object, err := preservedChatObject(choice, "type", "function", "name")
		if err != nil {
			return nil, fmt.Errorf("tool_choice: %w", err)
		}
		typ, err := preservedChatString(object["type"])
		if err != nil || typ != "function" {
			return nil, fmt.Errorf("tool_choice: unsupported selection type")
		}
		nameRaw = object["name"]
		if nested, ok := object["function"]; ok {
			if nameRaw != nil {
				return nil, fmt.Errorf("tool_choice: cannot preserve both flat and nested function names")
			}
			function, err := preservedChatObject(nested, "name")
			if err != nil {
				return nil, fmt.Errorf("tool_choice.function: %w", err)
			}
			nameRaw = function["name"]
		}
	}
	name, err := preservedChatString(nameRaw)
	if err != nil || strings.TrimSpace(name) == "" {
		return nil, fmt.Errorf("%s: function selection requires a name", field)
	}
	for _, tool := range tools {
		if tool.Type == "function" && tool.Name == name {
			return json.Marshal(map[string]string{"type": "function", "name": name})
		}
	}
	return nil, fmt.Errorf("%s: selected function is not declared", field)
}

// legacyFunctionCallIDs pairs legacy function calls with their results. Legacy
// function calling carries no call IDs: the assistant turn has function_call
// and the role=function result refers to it only by name, while the Responses
// API pairs function_call and function_call_output by call_id. IDs follow
// message order, so a replayed history converts to the same input, and skip
// IDs already used by tool_calls in the same conversation.
type legacyFunctionCallIDs struct {
	msgs    []ChatMessage
	used    map[string]bool
	next    int
	pending map[string][]string
}

// assign returns a new call ID for a legacy function call and queues it for
// the next result with the same function name.
func (ids *legacyFunctionCallIDs) assign(name string) string {
	if ids.used == nil {
		ids.used = make(map[string]bool)
		ids.pending = make(map[string][]string)
		for _, m := range ids.msgs {
			for _, tc := range m.ToolCalls {
				ids.used[tc.ID] = true
			}
			if m.ToolCallID != "" {
				ids.used[m.ToolCallID] = true
			}
		}
	}
	var id string
	for {
		ids.next++
		id = fmt.Sprintf("call_legacy_%d", ids.next)
		if !ids.used[id] {
			break
		}
	}
	ids.used[id] = true
	ids.pending[name] = append(ids.pending[name], id)
	return id
}

// claim returns the oldest unanswered call ID for name, or "" when no legacy
// call with that name precedes the result.
func (ids *legacyFunctionCallIDs) claim(name string) string {
	queue := ids.pending[name]
	if len(queue) == 0 {
		return ""
	}
	ids.pending[name] = queue[1:]
	return queue[0]
}

// parseAssistantContent returns assistant content as plain text.
//
// Supported formats:
// - JSON string
// - JSON array of typed parts (e.g. [{"type":"text","text":"..."}])
//
// For structured thinking/reasoning parts, it preserves semantics by wrapping
// the text in explicit tags so downstream can still distinguish it from normal text.
func parseAssistantContent(raw json.RawMessage) (string, error) {
	if len(raw) == 0 {
		return "", nil
	}

	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		return s, nil
	}

	var parts []map[string]any
	if err := json.Unmarshal(raw, &parts); err != nil {
		// Keep compatibility with prior behavior: unsupported assistant content
		// formats are ignored instead of failing the whole request conversion.
		return "", nil
	}

	var b strings.Builder
	write := func(v string) error {
		_, err := b.WriteString(v)
		return err
	}
	for _, p := range parts {
		typ, _ := p["type"].(string)
		text, _ := p["text"].(string)
		thinking, _ := p["thinking"].(string)

		switch typ {
		case "thinking", "reasoning":
			if thinking != "" {
				if err := write("<thinking>"); err != nil {
					return "", err
				}
				if err := write(thinking); err != nil {
					return "", err
				}
				if err := write("</thinking>"); err != nil {
					return "", err
				}
			} else if text != "" {
				if err := write("<thinking>"); err != nil {
					return "", err
				}
				if err := write(text); err != nil {
					return "", err
				}
				if err := write("</thinking>"); err != nil {
					return "", err
				}
			}
		default:
			if text != "" {
				if err := write(text); err != nil {
					return "", err
				}
			}
		}
	}

	return b.String(), nil
}

// parseChatContent returns the string value of a ChatMessage Content field.
// Content can be a JSON string or an array of typed parts. Array content is
// flattened to text by concatenating text parts and ignoring non-text parts.
func parseChatContent(raw json.RawMessage) (string, error) {
	parsed, err := parseChatMessageContent(raw)
	if err != nil {
		return "", err
	}
	if parsed.Text != nil {
		return *parsed.Text, nil
	}
	return flattenChatContentParts(parsed.Parts), nil
}

func parseChatMessageContent(raw json.RawMessage) (chatMessageContent, error) {
	if len(raw) == 0 {
		return chatMessageContent{Text: stringPtr("")}, nil
	}

	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		return chatMessageContent{Text: &s}, nil
	}

	var parts []ChatContentPart
	if err := json.Unmarshal(raw, &parts); err == nil {
		return chatMessageContent{Parts: parts}, nil
	}

	return chatMessageContent{}, fmt.Errorf("parse content as string or parts array")
}

func marshalChatInputContent(content chatMessageContent) (json.RawMessage, error) {
	if content.Text != nil {
		return json.Marshal(*content.Text)
	}
	parts := convertChatContentPartsToResponses(content.Parts)
	if len(parts) == 0 {
		// A nil slice marshals to JSON null, which the upstream Responses API
		// rejects ("expected an array of objects or string, but got null").
		// Fall back to an empty string when no usable parts remain.
		return json.Marshal("")
	}
	return json.Marshal(parts)
}

func convertChatContentPartsToResponses(parts []ChatContentPart) []ResponsesContentPart {
	var responseParts []ResponsesContentPart
	for _, p := range parts {
		switch p.Type {
		case "text":
			if p.Text != "" || len(p.PromptCacheBreakpoint) > 0 {
				responseParts = append(responseParts, ResponsesContentPart{
					PromptCacheBreakpoint: p.PromptCacheBreakpoint,
					Type:                  "input_text",
					Text:                  p.Text,
				})
			}
		case "image_url":
			if p.ImageURL != nil && p.ImageURL.URL != "" && !isEmptyBase64DataURI(p.ImageURL.URL) {
				responseParts = append(responseParts, ResponsesContentPart{
					PromptCacheBreakpoint: p.PromptCacheBreakpoint,
					Type:                  "input_image",
					ImageURL:              p.ImageURL.URL,
				})
			}
		case "file":
			if p.File != nil && (p.File.FileData != "" || p.File.FileID != "") {
				responseParts = append(responseParts, ResponsesContentPart{
					PromptCacheBreakpoint: p.PromptCacheBreakpoint,
					Type:                  "input_file",
					Filename:              p.File.Filename,
					FileData:              p.File.FileData,
					FileID:                p.File.FileID,
				})
			}
		}
	}
	return responseParts
}

func isEmptyBase64DataURI(raw string) bool {
	if !strings.HasPrefix(raw, "data:") {
		return false
	}
	rest := strings.TrimPrefix(raw, "data:")
	semicolonIdx := strings.Index(rest, ";")
	if semicolonIdx < 0 {
		return false
	}
	rest = rest[semicolonIdx+1:]
	if !strings.HasPrefix(rest, "base64,") {
		return false
	}
	return strings.TrimSpace(strings.TrimPrefix(rest, "base64,")) == ""
}

func flattenChatContentParts(parts []ChatContentPart) string {
	var textParts []string
	for _, p := range parts {
		if p.Type == "text" && p.Text != "" {
			textParts = append(textParts, p.Text)
		}
	}
	return strings.Join(textParts, "")
}

func stringPtr(s string) *string {
	return &s
}

func defaultStrictFalse(src *bool) *bool {
	if src == nil {
		value := false
		return &value
	}
	return src
}
