package apicompat

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
)

type AnthropicConversionMode uint8

const (
	AnthropicConversionCompatible AnthropicConversionMode = iota
	AnthropicConversionFixedCodexHTTP
)

// AnthropicToResponses converts an Anthropic Messages request directly into
// a Responses API request. This preserves fields that would be lost in a
// Chat Completions intermediary round-trip (e.g. thinking, cache_control,
// structured system prompts).
func AnthropicToResponses(req *AnthropicRequest) (*ResponsesRequest, error) {
	return AnthropicToResponsesWithMinOutputTokens(req, DefaultMinMaxOutputTokens)
}

// AnthropicToResponsesWithMinOutputTokens converts an Anthropic request using
// the supplied max_output_tokens floor.
func AnthropicToResponsesWithMinOutputTokens(req *AnthropicRequest, minOutputTokens int) (*ResponsesRequest, error) {
	return AnthropicToResponsesWithOptions(req, AnthropicToResponsesOptions{MinOutputTokens: minOutputTokens})
}

// AnthropicToResponsesOptions controls conversion and optional deferred effort
// validation. Callers deferring validation must validate raw selections before
// conversion and the final effort after applying their policy.
type AnthropicToResponsesOptions struct {
	MinOutputTokens                int
	DeferReasoningEffortValidation bool
	Mode                           AnthropicConversionMode
	OriginalRequest                json.RawMessage
}

type anthropicResponsesInputItem struct {
	ResponsesInputItem
	Summary         *[]ResponsesSummary `json:"summary,omitempty"`
	codexToolOutput json.RawMessage
}

func (item anthropicResponsesInputItem) MarshalJSON() ([]byte, error) {
	type wireItem anthropicResponsesInputItem
	if item.codexToolOutput != nil {
		return json.Marshal(struct {
			wireItem
			Output json.RawMessage `json:"output"`
		}{wireItem(item), item.codexToolOutput})
	}
	return json.Marshal(wireItem(item))
}

func AnthropicToResponsesWithOptions(req *AnthropicRequest, options AnthropicToResponsesOptions) (*ResponsesRequest, error) {
	if !options.DeferReasoningEffortValidation {
		if err := openai.ValidateGPT61ReasoningEffort(req.Model, anthropicReasoningEffort(req)); err != nil {
			return nil, err
		}
	}
	strict := options.Mode == AnthropicConversionFixedCodexHTTP
	if strict {
		if err := validateAnthropicCodexRequest(req, options.OriginalRequest); err != nil {
			return nil, err
		}
		if err := validateAnthropicCodexTools(req.Tools, options.OriginalRequest); err != nil {
			return nil, err
		}
	}
	input, err := convertAnthropicToResponsesInput(req.System, req.Messages, options.Mode)
	if err != nil {
		return nil, err
	}
	if strict {
		if err := validateAnthropicCodexToolReferences(input); err != nil {
			return nil, err
		}
	}

	inputJSON, err := json.Marshal(input)
	if err != nil {
		return nil, err
	}

	out := &ResponsesRequest{
		Model:   req.Model,
		Input:   inputJSON,
		Stream:  req.Stream,
		Include: []string{"reasoning.encrypted_content"},
	}

	// Reasoning models (gpt-5.x) served via the Responses API do not accept
	// sampling parameters. Sending temperature or top_p causes a 400
	// "Unsupported parameter" error, so we only forward them for non-reasoning
	// models.
	if !isReasoningModel(req.Model) {
		out.Temperature = req.Temperature
		out.TopP = req.TopP
	}

	storeFalse := false
	out.Store = &storeFalse
	parallelToolCalls := true
	out.ParallelToolCalls = &parallelToolCalls
	out.Text = &ResponsesText{Verbosity: "medium"}

	if req.MaxTokens > 0 {
		v := req.MaxTokens
		if v < options.MinOutputTokens {
			v = options.MinOutputTokens
		}
		out.MaxOutputTokens = &v
	}

	if len(req.Tools) > 0 {
		out.Tools = convertAnthropicToolsToResponsesWithMode(req.Tools, options.Mode)
	}

	// An explicit thinking disable takes precedence over output_config.effort.
	effort := anthropicReasoningEffort(req)
	if (openai.GPT62ModelID(req.Model) != "" || openai.IsGPT61SolModelSpelling(req.Model) || openai.IsGPT61TerraModelSpelling(req.Model)) && req.OutputConfig != nil && req.OutputConfig.Effort == "max" && effort != "none" {
		effort = "max"
	}
	out.Reasoning = &ResponsesReasoning{
		Effort: effort,
	}
	if effort != "none" {
		out.Reasoning.Summary = "auto"
	}

	// Convert tool_choice
	if len(req.ToolChoice) > 0 {
		if strict {
			parallel, err := validateAnthropicCodexToolChoice(req.ToolChoice, req.Tools)
			if err != nil {
				return nil, err
			}
			out.ParallelToolCalls = &parallel
		}
		tc, err := convertAnthropicToolChoiceToResponses(req.ToolChoice)
		if err != nil {
			return nil, fmt.Errorf("convert tool_choice: %w", err)
		}
		out.ToolChoice = tc
	}

	return out, nil
}

// convertAnthropicToolChoiceToResponses maps Anthropic tool_choice to Responses format.
//
//	{"type":"auto"}            → "auto"
//	{"type":"any"}             → "required"
//	{"type":"none"}            → "none"
//	{"type":"tool","name":"X"} → {"type":"function","name":"X"}
func convertAnthropicToolChoiceToResponses(raw json.RawMessage) (json.RawMessage, error) {
	var tc struct {
		Type string `json:"type"`
		Name string `json:"name"`
	}
	if err := json.Unmarshal(raw, &tc); err != nil {
		return nil, err
	}

	switch tc.Type {
	case "auto":
		return json.Marshal("auto")
	case "any":
		return json.Marshal("required")
	case "none":
		return json.Marshal("none")
	case "tool":
		return json.Marshal(map[string]any{
			"type": "function",
			"name": tc.Name,
		})
	default:
		// Pass through unknown types as-is
		return raw, nil
	}
}

// convertAnthropicToResponsesInput builds the Responses API input items array
// from the Anthropic system field and message list.
func convertAnthropicToResponsesInput(
	system json.RawMessage,
	msgs []AnthropicMessage,
	mode AnthropicConversionMode,
) ([]anthropicResponsesInputItem, error) {
	if mode == AnthropicConversionFixedCodexHTTP {
		return convertAnthropicCodexInput(system, msgs)
	}
	var out []anthropicResponsesInputItem

	// System prompt → developer role input item. ChatGPT Codex SSE behaves like
	// Codex CLI here: keeping Anthropic system text in input preserves the
	// conversation/cache shape better than moving it into instructions.
	if len(system) > 0 {
		sysParts, err := parseAnthropicSystemContentParts(system)
		if err != nil {
			return nil, err
		}
		if len(sysParts) > 0 {
			content, _ := json.Marshal(sysParts)
			out = append(out, anthropicResponsesInputItem{ResponsesInputItem: ResponsesInputItem{
				Type: "message", Role: "developer", Content: content,
			}})
		}
	}

	for _, m := range msgs {
		items, err := anthropicMsgToResponsesItems(m)
		if err != nil {
			return nil, err
		}
		out = append(out, items...)
	}
	return out, nil
}

// parseAnthropicSystemContentParts handles the Anthropic system field which can
// be a plain string or an array of text blocks. Claude Code may include an
// x-anthropic-billing-header block; airgate drops it before sending to Codex.
func parseAnthropicSystemContentParts(raw json.RawMessage) ([]ResponsesContentPart, error) {
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		if isAnthropicBillingHeaderText(s) || s == "" {
			return nil, nil
		}
		return []ResponsesContentPart{{Type: "input_text", Text: s}}, nil
	}
	var blocks []AnthropicContentBlock
	if err := json.Unmarshal(raw, &blocks); err != nil {
		return nil, err
	}
	var parts []ResponsesContentPart
	for _, b := range blocks {
		if b.Type == "text" && b.Text != "" && !isAnthropicBillingHeaderText(b.Text) {
			parts = append(parts, ResponsesContentPart{Type: "input_text", Text: b.Text})
		}
	}
	return parts, nil
}

func isAnthropicBillingHeaderText(text string) bool {
	return strings.HasPrefix(text, "x-anthropic-billing-header: ")
}

// anthropicMsgToResponsesItems converts a single Anthropic message into one
// or more Responses API input items.
func anthropicMsgToResponsesItems(m AnthropicMessage) ([]anthropicResponsesInputItem, error) {
	switch m.Role {
	case "user":
		items, err := anthropicUserToResponses(m.Content)
		if err != nil {
			return nil, err
		}
		return wrapAnthropicResponsesInputItems(items), nil
	case "assistant":
		return anthropicAssistantToResponses(m.Content)
	case "system":
		// Mid-conversation system turns (e.g. hook output) keep system-level
		// authority: Responses uses developer for them, not user.
		items, err := anthropicUserToResponses(m.Content)
		for i := range items {
			if items[i].Role == "user" {
				items[i].Role = "developer"
			}
		}
		return wrapAnthropicResponsesInputItems(items), err
	default:
		items, err := anthropicUserToResponses(m.Content)
		return wrapAnthropicResponsesInputItems(items), err
	}
}

func wrapAnthropicResponsesInputItems(items []ResponsesInputItem) []anthropicResponsesInputItem {
	out := make([]anthropicResponsesInputItem, 0, len(items))
	for _, item := range items {
		out = append(out, anthropicResponsesInputItem{ResponsesInputItem: item})
	}
	return out
}

// anthropicUserToResponses handles an Anthropic user message. Content can be a
// plain string or an array of blocks. tool_result blocks are extracted into
// function_call_output items. Image blocks are converted to input_image parts.
func anthropicUserToResponses(raw json.RawMessage) ([]ResponsesInputItem, error) {
	// Try plain string.
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		parts := []ResponsesContentPart{{Type: "input_text", Text: s}}
		partsJSON, err := json.Marshal(parts)
		if err != nil {
			return nil, err
		}
		return []ResponsesInputItem{{Type: "message", Role: "user", Content: partsJSON}}, nil
	}

	var blocks []AnthropicContentBlock
	if err := json.Unmarshal(raw, &blocks); err != nil {
		return nil, err
	}

	var out []ResponsesInputItem
	var toolResultImageParts []ResponsesContentPart

	// Extract tool_result blocks → function_call_output items.
	// Images inside tool_results are extracted separately because the
	// Responses API function_call_output.output only accepts strings.
	for _, b := range blocks {
		if b.Type != "tool_result" {
			continue
		}
		outputText, imageParts := convertToolResultOutput(b)
		out = append(out, ResponsesInputItem{
			Type:   "function_call_output",
			CallID: toResponsesCallID(b.ToolUseID),
			Output: outputText,
		})
		toolResultImageParts = append(toolResultImageParts, imageParts...)
	}

	// Remaining text + image blocks → user message with content parts.
	// Also include images extracted from tool_results so the model can see them.
	var parts []ResponsesContentPart
	for _, b := range blocks {
		switch b.Type {
		case "text":
			if b.Text != "" {
				parts = append(parts, ResponsesContentPart{Type: "input_text", Text: b.Text})
			}
		case "image":
			if uri := anthropicImageToDataURI(b.Source); uri != "" {
				parts = append(parts, ResponsesContentPart{Type: "input_image", ImageURL: uri})
			}
		}
	}
	parts = append(parts, toolResultImageParts...)

	if len(parts) > 0 {
		content, err := json.Marshal(parts)
		if err != nil {
			return nil, err
		}
		out = append(out, ResponsesInputItem{Type: "message", Role: "user", Content: content})
	}

	return out, nil
}

// anthropicAssistantToResponses handles an Anthropic assistant message.
// Text content → assistant message with output_text parts.
// tool_use blocks → function_call items.
// thinking blocks with signature → reasoning items (encrypted_content) so
// multi-turn Grok/Codex prompt cache can reuse prior reasoning prefixes.
// thinking without signature remains ignored (not accepted as plain text input).
func anthropicAssistantToResponses(raw json.RawMessage) ([]anthropicResponsesInputItem, error) {
	// Try plain string.
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		parts := []ResponsesContentPart{{Type: "output_text", Text: s}}
		partsJSON, err := json.Marshal(parts)
		if err != nil {
			return nil, err
		}
		return []anthropicResponsesInputItem{{ResponsesInputItem: ResponsesInputItem{
			Type: "message", Role: "assistant", Content: partsJSON,
		}}}, nil
	}

	var blocks []AnthropicContentBlock
	if err := json.Unmarshal(raw, &blocks); err != nil {
		return nil, err
	}

	var items []anthropicResponsesInputItem

	// Preserve turn order: reasoning → assistant text → tool calls. xAI/Codex
	// multi-turn cache and tool continuations expect reasoning before the
	// assistant message that followed it.
	for _, b := range blocks {
		if b.Type != "thinking" {
			continue
		}
		sig := strings.TrimSpace(b.Signature)
		// Only replay provider ciphertext. Skip GPT/Codex-style gAAAA blobs and
		// empty placeholders — xAI returns 400 on decrypt for foreign signatures.
		if sig == "" || strings.HasPrefix(sig, "gAAAA") {
			continue
		}
		// CC Switch carries a JSON reasoning item in this versioned envelope,
		// not provider ciphertext. Ignore the foreign state when resuming directly.
		if strings.HasPrefix(sig, "ccswitch-openai-reasoning-v1:") {
			continue
		}
		items = append(items, anthropicResponsesInputItem{ResponsesInputItem: ResponsesInputItem{
			Type:             "reasoning",
			EncryptedContent: sig,
		}})
	}

	// Text content → assistant message with output_text content parts.
	text := extractAnthropicTextFromBlocks(blocks)
	if text != "" {
		parts := []ResponsesContentPart{{Type: "output_text", Text: text}}
		partsJSON, err := json.Marshal(parts)
		if err != nil {
			return nil, err
		}
		items = append(items, anthropicResponsesInputItem{ResponsesInputItem: ResponsesInputItem{
			Type: "message", Role: "assistant", Content: partsJSON,
		}})
	}

	// tool_use → function_call items.
	for _, b := range blocks {
		if b.Type != "tool_use" {
			continue
		}
		args := "{}"
		if len(b.Input) > 0 {
			args = string(b.Input)
		}
		fcID := toResponsesCallID(b.ID)
		items = append(items, anthropicResponsesInputItem{ResponsesInputItem: ResponsesInputItem{
			Type:      "function_call",
			CallID:    fcID,
			Name:      b.Name,
			Arguments: args,
		}})
	}

	return items, nil
}

type anthropicCodexContentPart struct {
	Type     string  `json:"type"`
	Text     *string `json:"text,omitempty"`
	ImageURL string  `json:"image_url,omitempty"`
}

func anthropicCodexContentError(path, reason string) error {
	return fmt.Errorf("codex content incompatible: %s: %s", path, reason)
}

func validateAnthropicCodexRequest(req *AnthropicRequest, original json.RawMessage) error {
	if req.Thinking != nil {
		switch req.Thinking.Type {
		case "enabled", "adaptive", "disabled", "between_tools":
		default:
			return anthropicCodexContentError("thinking.type", "unsupported thinking mode")
		}
		if req.Thinking.BudgetTokens < 0 {
			return anthropicCodexContentError(
				"thinking.budget_tokens",
				"expected a nonnegative integer",
			)
		}
	}
	if req.OutputConfig != nil {
		switch req.OutputConfig.Effort {
		case "", "low", "medium", "high", "max":
		default:
			return anthropicCodexContentError(
				"output_config.effort",
				"unsupported reasoning effort",
			)
		}
	}
	if len(req.Metadata) > 0 {
		if _, err := anthropicCodexObject(req.Metadata, "metadata"); err != nil {
			return err
		}
	}
	if len(original) == 0 {
		return nil
	}

	request, err := anthropicCodexObject(original, "request")
	if err != nil {
		return err
	}
	for field, allowed := range map[string][]string{
		"thinking":      {"type", "budget_tokens"},
		"output_config": {"effort"},
	} {
		raw, exists := request[field]
		if !exists {
			continue
		}
		fields, err := anthropicCodexObject(raw, field)
		if err != nil {
			return err
		}
		if err := anthropicCodexFields(fields, field, allowed...); err != nil {
			return err
		}
		if field == "thinking" {
			typ, err := anthropicCodexString(fields, "type", field, true)
			if err != nil {
				return err
			}
			switch typ {
			case "enabled", "adaptive", "disabled", "between_tools":
			default:
				return anthropicCodexContentError(
					"thinking.type",
					"unsupported thinking mode",
				)
			}
			if budget, exists := fields["budget_tokens"]; exists {
				var value *int
				if err := json.Unmarshal(budget, &value); err != nil || value == nil || *value < 0 {
					return anthropicCodexContentError(
						"thinking.budget_tokens",
						"expected a nonnegative integer",
					)
				}
			}
		}
		if field == "output_config" {
			if rawEffort, exists := fields["effort"]; exists {
				var effort *string
				if err := json.Unmarshal(rawEffort, &effort); err != nil || effort == nil {
					return anthropicCodexContentError(
						"output_config.effort",
						"expected a string",
					)
				}
				switch *effort {
				case "", "low", "medium", "high", "max":
				default:
					return anthropicCodexContentError(
						"output_config.effort",
						"unsupported reasoning effort",
					)
				}
			}
		}
	}
	if raw, exists := request["metadata"]; exists {
		if _, err := anthropicCodexObject(raw, "metadata"); err != nil {
			return err
		}
	}
	if raw, exists := request["service_tier"]; exists {
		var value *string
		if err := json.Unmarshal(raw, &value); err != nil || value == nil {
			return anthropicCodexContentError("service_tier", "expected a string")
		}
	}
	return nil
}

func anthropicCodexObject(raw json.RawMessage, path string) (map[string]json.RawMessage, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil || fields == nil {
		return nil, anthropicCodexContentError(path, "expected an object")
	}
	return fields, nil
}

func anthropicCodexFields(fields map[string]json.RawMessage, path string, allowed ...string) error {
	for key := range fields {
		found := false
		for _, name := range allowed {
			if key == name {
				found = true
				break
			}
		}
		if !found {
			return anthropicCodexContentError(path+"."+key, "unsupported field")
		}
	}
	return nil
}

func anthropicCodexString(fields map[string]json.RawMessage, key, path string, nonblank bool) (string, error) {
	var value *string
	if err := json.Unmarshal(fields[key], &value); err != nil || value == nil {
		return "", anthropicCodexContentError(path+"."+key, "expected a string")
	}
	if nonblank && strings.TrimSpace(*value) == "" {
		return "", anthropicCodexContentError(path+"."+key, "expected a nonblank string")
	}
	return *value, nil
}

func anthropicCodexBlocks(raw json.RawMessage, path string) ([]json.RawMessage, error) {
	var text *string
	if err := json.Unmarshal(raw, &text); err == nil && text != nil {
		block, err := json.Marshal(AnthropicContentBlock{Type: "text", Text: *text})
		return []json.RawMessage{block}, err
	}
	var blocks []json.RawMessage
	if err := json.Unmarshal(raw, &blocks); err != nil || blocks == nil {
		return nil, anthropicCodexContentError(path, "expected a string or content block array")
	}
	return blocks, nil
}

func anthropicCodexPart(
	fields map[string]json.RawMessage,
	typ string,
	path string,
	images bool,
) (anthropicCodexContentPart, error) {
	var part anthropicCodexContentPart
	switch typ {
	case "text":
		if err := anthropicCodexFields(fields, path, "type", "text", "cache_control"); err != nil {
			return part, err
		}
		text, err := anthropicCodexString(fields, "text", path, false)
		return anthropicCodexContentPart{Type: "input_text", Text: &text}, err
	case "image":
		if !images {
			return part, anthropicCodexContentError(path, "image content is not supported in this role")
		}
		if err := anthropicCodexFields(fields, path, "type", "source", "cache_control"); err != nil {
			return part, err
		}
		sourcePath := path + ".source"
		source, err := anthropicCodexObject(fields["source"], sourcePath)
		if err != nil {
			return part, err
		}
		sourceType, err := anthropicCodexString(source, "type", sourcePath, true)
		if err != nil {
			return part, err
		}
		part.Type = "input_image"
		switch sourceType {
		case "base64":
			if err := anthropicCodexFields(source, sourcePath, "type", "media_type", "data"); err != nil {
				return part, err
			}
			mediaType, err := anthropicCodexString(source, "media_type", sourcePath, true)
			if err != nil {
				return part, err
			}
			if !strings.HasPrefix(strings.ToLower(mediaType), "image/") {
				return part, anthropicCodexContentError(sourcePath+".media_type", "expected an image media type")
			}
			data, err := anthropicCodexString(source, "data", sourcePath, true)
			if err != nil {
				return part, err
			}
			part.ImageURL = "data:" + mediaType + ";base64," + data
			return part, nil
		case "url":
			if err := anthropicCodexFields(source, sourcePath, "type", "url"); err != nil {
				return part, err
			}
			part.ImageURL, err = anthropicCodexString(source, "url", sourcePath, true)
			return part, err
		default:
			return part, anthropicCodexContentError(sourcePath+".type", "unsupported image source")
		}
	default:
		return part, anthropicCodexContentError(path, "unsupported content block type")
	}
}

func anthropicCodexParts(
	raw json.RawMessage,
	path string,
	images bool,
) ([]anthropicCodexContentPart, error) {
	blocks, err := anthropicCodexBlocks(raw, path)
	if err != nil {
		return nil, err
	}
	parts := make([]anthropicCodexContentPart, 0, len(blocks))
	for i, block := range blocks {
		blockPath := fmt.Sprintf("%s[%d]", path, i)
		fields, err := anthropicCodexObject(block, blockPath)
		if err != nil {
			return nil, err
		}
		typ, err := anthropicCodexString(fields, "type", blockPath, true)
		if err != nil {
			return nil, err
		}
		part, err := anthropicCodexPart(fields, typ, blockPath, images)
		if err != nil {
			return nil, err
		}
		parts = append(parts, part)
	}
	return parts, nil
}

func convertAnthropicCodexInput(
	system json.RawMessage,
	messages []AnthropicMessage,
) ([]anthropicResponsesInputItem, error) {
	var out []anthropicResponsesInputItem
	if len(system) > 0 {
		parts, err := anthropicCodexParts(system, "system", false)
		if err != nil {
			return nil, err
		}
		content, err := json.Marshal(parts)
		if err != nil {
			return nil, err
		}
		out = append(out, anthropicResponsesInputItem{ResponsesInputItem: ResponsesInputItem{
			Type: "message", Role: "developer", Content: content,
		}})
	}
	for i, message := range messages {
		path := fmt.Sprintf("messages[%d]", i)
		if message.Role != "user" && message.Role != "assistant" {
			return nil, anthropicCodexContentError(path+".role", "unsupported message role")
		}
		items, err := anthropicCodexMessage(message, path+".content")
		if err != nil {
			return nil, err
		}
		out = append(out, items...)
	}
	return out, nil
}

func anthropicCodexMessage(
	message AnthropicMessage,
	path string,
) ([]anthropicResponsesInputItem, error) {
	blocks, err := anthropicCodexBlocks(message.Content, path)
	if err != nil {
		return nil, err
	}
	var items []anthropicResponsesInputItem
	var parts []anthropicCodexContentPart
	flush := func() error {
		if len(parts) == 0 {
			return nil
		}
		content, err := json.Marshal(parts)
		if err != nil {
			return err
		}
		items = append(items, anthropicResponsesInputItem{ResponsesInputItem: ResponsesInputItem{
			Type: "message", Role: message.Role, Content: content,
		}})
		parts = nil
		return nil
	}

	for i, block := range blocks {
		blockPath := fmt.Sprintf("%s[%d]", path, i)
		fields, err := anthropicCodexObject(block, blockPath)
		if err != nil {
			return nil, err
		}
		typ, err := anthropicCodexString(fields, "type", blockPath, true)
		if err != nil {
			return nil, err
		}
		if typ == "text" || typ == "image" {
			part, err := anthropicCodexPart(fields, typ, blockPath, message.Role == "user")
			if err != nil {
				return nil, err
			}
			if message.Role == "assistant" {
				part.Type = "output_text"
			}
			parts = append(parts, part)
			continue
		}
		if err := flush(); err != nil {
			return nil, err
		}

		item := anthropicResponsesInputItem{}
		switch {
		case typ == "thinking" && message.Role == "assistant":
			if err := anthropicCodexFields(
				fields,
				blockPath,
				"type",
				"thinking",
				"signature",
				"cache_control",
			); err != nil {
				return nil, err
			}
			item.Type = "reasoning"
			item.EncryptedContent, err = anthropicCodexString(fields, "signature", blockPath, true)
			if err != nil {
				return nil, err
			}
			summary := []ResponsesSummary{}
			if _, exists := fields["thinking"]; exists {
				thinking, err := anthropicCodexString(fields, "thinking", blockPath, false)
				if err != nil {
					return nil, err
				}
				if thinking != "" {
					summary = append(summary, ResponsesSummary{Type: "summary_text", Text: thinking})
				}
			}
			item.Summary = &summary
		case typ == "tool_use" && message.Role == "assistant":
			if err := anthropicCodexFields(
				fields,
				blockPath,
				"type",
				"id",
				"name",
				"input",
				"cache_control",
			); err != nil {
				return nil, err
			}
			item.Type = "function_call"
			item.CallID, err = anthropicCodexString(fields, "id", blockPath, true)
			if err != nil {
				return nil, err
			}
			item.Name, err = anthropicCodexString(fields, "name", blockPath, true)
			if err != nil {
				return nil, err
			}
			if _, err := anthropicCodexObject(fields["input"], blockPath+".input"); err != nil {
				return nil, err
			}
			item.Arguments = string(fields["input"])
		case typ == "tool_result" && message.Role == "user":
			if err := anthropicCodexFields(
				fields,
				blockPath,
				"type",
				"tool_use_id",
				"content",
				"is_error",
				"cache_control",
			); err != nil {
				return nil, err
			}
			item.Type = "function_call_output"
			item.CallID, err = anthropicCodexString(fields, "tool_use_id", blockPath, true)
			if err != nil {
				return nil, err
			}
			item.codexToolOutput, err = anthropicCodexToolOutput(fields, blockPath)
			if err != nil {
				return nil, err
			}
		default:
			return nil, anthropicCodexContentError(
				blockPath,
				"unsupported content block type for this role",
			)
		}
		items = append(items, item)
	}
	if err := flush(); err != nil {
		return nil, err
	}
	return items, nil
}

func anthropicCodexToolOutput(
	fields map[string]json.RawMessage,
	path string,
) (json.RawMessage, error) {
	content, exists := fields["content"]
	if !exists {
		content = json.RawMessage(`""`)
	}
	isError := false
	if raw, exists := fields["is_error"]; exists {
		var value *bool
		if err := json.Unmarshal(raw, &value); err != nil || value == nil {
			return nil, anthropicCodexContentError(path+".is_error", "expected a boolean")
		}
		isError = *value
	}

	var text *string
	if err := json.Unmarshal(content, &text); err == nil && text != nil && !isError {
		return json.Marshal(*text)
	}
	parts, err := anthropicCodexParts(content, path+".content", true)
	if err != nil {
		return nil, err
	}
	if isError {
		status := `{"is_error":true}`
		parts = append(
			[]anthropicCodexContentPart{{Type: "input_text", Text: &status}},
			parts...,
		)
	}
	return json.Marshal(parts)
}

func validateAnthropicCodexToolReferences(items []anthropicResponsesInputItem) error {
	calls := make(map[string]struct{})
	outputs := make(map[string]struct{})
	for index, item := range items {
		switch item.Type {
		case "function_call":
			callID := strings.TrimSpace(item.CallID)
			if callID == "" {
				return anthropicCodexContentError(
					fmt.Sprintf("input[%d].call_id", index),
					"expected a nonblank call id",
				)
			}
			if _, exists := calls[callID]; exists {
				return anthropicCodexContentError(
					fmt.Sprintf("input[%d].call_id", index),
					"duplicate tool call id "+callID,
				)
			}
			calls[callID] = struct{}{}
		case "function_call_output":
			callID := strings.TrimSpace(item.CallID)
			if _, exists := calls[callID]; !exists {
				return anthropicCodexContentError(
					fmt.Sprintf("input[%d].call_id", index),
					"tool result has no matching call "+callID,
				)
			}
			if _, exists := outputs[callID]; exists {
				return anthropicCodexContentError(
					fmt.Sprintf("input[%d].call_id", index),
					"duplicate tool result id "+callID,
				)
			}
			outputs[callID] = struct{}{}
		}
	}
	return nil
}

func validateAnthropicCodexTools(tools []AnthropicTool, original json.RawMessage) error {
	if len(original) > 0 {
		request, err := anthropicCodexObject(original, "request")
		if err != nil {
			return err
		}
		if raw, exists := request["tools"]; exists {
			var definitions []json.RawMessage
			if err := json.Unmarshal(raw, &definitions); err != nil || definitions == nil {
				return anthropicCodexContentError("tools", "expected an array")
			}
			for i, definition := range definitions {
				path := fmt.Sprintf("tools[%d]", i)
				fields, err := anthropicCodexObject(definition, path)
				if err != nil {
					return err
				}
				if err := anthropicCodexFields(
					fields,
					path,
					"type",
					"name",
					"description",
					"input_schema",
					"cache_control",
				); err != nil {
					return err
				}
			}
		}
	}

	names := make(map[string]bool, len(tools))
	for i, tool := range tools {
		path := fmt.Sprintf("tools[%d]", i)
		if tool.Type != "" && tool.Type != "custom" {
			return anthropicCodexContentError(path+".type", "unsupported tool type")
		}
		if strings.TrimSpace(tool.Name) == "" {
			return anthropicCodexContentError(path+".name", "expected a nonblank tool name")
		}
		if names[tool.Name] {
			return anthropicCodexContentError(path+".name", "expected a unique tool name")
		}
		names[tool.Name] = true

		schemaPath := path + ".input_schema"
		schema, err := anthropicCodexObject(tool.InputSchema, schemaPath)
		if err != nil {
			return err
		}
		typ, err := anthropicCodexString(schema, "type", schemaPath, true)
		if err != nil {
			return err
		}
		if typ != "object" {
			return anthropicCodexContentError(schemaPath+".type", "expected an object schema")
		}
		if raw, exists := schema["properties"]; exists {
			if _, err := anthropicCodexObject(raw, schemaPath+".properties"); err != nil {
				return err
			}
		}
		if raw, exists := schema["required"]; exists {
			var required []*string
			if err := json.Unmarshal(raw, &required); err != nil || required == nil {
				return anthropicCodexContentError(
					schemaPath+".required",
					"expected an array of strings",
				)
			}
			for _, name := range required {
				if name == nil {
					return anthropicCodexContentError(
						schemaPath+".required",
						"expected an array of strings",
					)
				}
			}
		}
	}
	return nil
}

func validateAnthropicCodexToolChoice(
	raw json.RawMessage,
	tools []AnthropicTool,
) (bool, error) {
	const path = "tool_choice"
	fields, err := anthropicCodexObject(raw, path)
	if err != nil {
		return false, err
	}
	if err := anthropicCodexFields(
		fields,
		path,
		"type",
		"name",
		"disable_parallel_tool_use",
	); err != nil {
		return false, err
	}
	typ, err := anthropicCodexString(fields, "type", path, true)
	if err != nil {
		return false, err
	}
	switch typ {
	case "auto", "any", "none":
		if _, exists := fields["name"]; exists {
			return false, anthropicCodexContentError(
				path+".name",
				"name requires a named tool choice",
			)
		}
	case "tool":
		name, err := anthropicCodexString(fields, "name", path, true)
		if err != nil {
			return false, err
		}
		found := false
		for _, tool := range tools {
			found = found || tool.Name == name
		}
		if !found {
			return false, anthropicCodexContentError(path+".name", "tool is not declared")
		}
	default:
		return false, anthropicCodexContentError(path+".type", "unsupported tool choice")
	}
	if raw, exists := fields["disable_parallel_tool_use"]; exists {
		var disabled *bool
		if err := json.Unmarshal(raw, &disabled); err != nil || disabled == nil {
			return false, anthropicCodexContentError(
				path+".disable_parallel_tool_use",
				"expected a boolean",
			)
		}
		return !*disabled, nil
	}
	return true, nil
}

// toResponsesCallID preserves Anthropic tool IDs as Responses call_id values.
// Claude Code sends tool_result.tool_use_id back verbatim, and ChatGPT Codex
// continuation expects that call_id to match the original tool_use id.
func toResponsesCallID(id string) string {
	return id
}

// fromResponsesCallID reverses old prefixed IDs while preserving current IDs.
func fromResponsesCallID(id string) string {
	if after, ok := strings.CutPrefix(id, "fc_"); ok {
		// Only strip if the remainder doesn't look like it was already "fc_" prefixed.
		// E.g. "fc_toolu_xxx" → "toolu_xxx", "fc_call_xxx" → "call_xxx"
		if strings.HasPrefix(after, "toolu_") || strings.HasPrefix(after, "call_") {
			return after
		}
	}
	return id
}

// anthropicImageToDataURI converts an AnthropicImageSource to a data URI string.
// Returns "" if the source is nil or has no data.
func anthropicImageToDataURI(src *AnthropicImageSource) string {
	if src == nil || src.Data == "" {
		return ""
	}
	mediaType := src.MediaType
	if mediaType == "" {
		mediaType = "image/png"
	}
	return "data:" + mediaType + ";base64," + src.Data
}

// convertToolResultOutput extracts text and image content from a tool_result
// block. Returns the text as a string for the function_call_output Output
// field, plus any image parts that must be sent in a separate user message
// (the Responses API output field only accepts strings).
func convertToolResultOutput(b AnthropicContentBlock) (string, []ResponsesContentPart) {
	if len(b.Content) == 0 {
		return "(empty)", nil
	}

	// Try plain string content.
	var s string
	if err := json.Unmarshal(b.Content, &s); err == nil {
		if s == "" {
			s = "(empty)"
		}
		return s, nil
	}

	// Array of content blocks — may contain text and/or images.
	var inner []AnthropicContentBlock
	if err := json.Unmarshal(b.Content, &inner); err != nil {
		return "(empty)", nil
	}

	// Separate text (for function_call_output) from images (for user message).
	var textParts []string
	var imageParts []ResponsesContentPart
	for _, ib := range inner {
		switch ib.Type {
		case "text":
			if ib.Text != "" {
				textParts = append(textParts, ib.Text)
			}
		case "image":
			if uri := anthropicImageToDataURI(ib.Source); uri != "" {
				imageParts = append(imageParts, ResponsesContentPart{Type: "input_image", ImageURL: uri})
			}
		}
	}

	text := strings.Join(textParts, "\n\n")
	if text == "" {
		text = "(empty)"
	}
	return text, imageParts
}

// extractAnthropicTextFromBlocks joins all text blocks, ignoring thinking/
// tool_use/tool_result blocks.
func extractAnthropicTextFromBlocks(blocks []AnthropicContentBlock) string {
	var parts []string
	for _, b := range blocks {
		if b.Type == "text" && b.Text != "" {
			parts = append(parts, b.Text)
		}
	}
	return strings.Join(parts, "\n\n")
}

// anthropicReasoningEffort resolves the Anthropic request preference for both
// OpenAI bridges. Explicitly disabled thinking overrides output_config.effort;
// otherwise the bridge keeps its medium default.
func anthropicReasoningEffort(req *AnthropicRequest) string {
	if req.Thinking != nil && req.Thinking.Type == "disabled" {
		return "none"
	}
	effort := "medium"
	if req.OutputConfig != nil && req.OutputConfig.Effort != "" {
		effort = req.OutputConfig.Effort
	}
	return mapAnthropicEffortToResponses(effort)
}

// mapAnthropicEffortToResponses maps shared effort levels directly and maps
// Anthropic's max to OpenAI's xhigh.
func mapAnthropicEffortToResponses(effort string) string {
	if effort == "max" {
		return "xhigh"
	}
	return effort // low→low, medium→medium, high→high, unknown→passthrough
}

// convertAnthropicToolsToResponses maps Anthropic tool definitions to
// Responses API tools. Server-side tools like web_search are mapped to their
// OpenAI equivalents; regular tools become function tools.
func convertAnthropicToolsToResponses(tools []AnthropicTool) []ResponsesTool {
	return convertAnthropicToolsToResponsesWithMode(tools, AnthropicConversionCompatible)
}

func convertAnthropicToolsToResponsesWithMode(
	tools []AnthropicTool,
	mode AnthropicConversionMode,
) []ResponsesTool {
	var out []ResponsesTool
	for _, t := range tools {
		// Anthropic server tools like "web_search_20250305" → OpenAI {"type":"web_search"}
		if mode != AnthropicConversionFixedCodexHTTP && strings.HasPrefix(t.Type, "web_search") {
			out = append(out, ResponsesTool{Type: "web_search"})
			continue
		}
		parameters := normalizeToolParameters(t.InputSchema)
		if mode == AnthropicConversionFixedCodexHTTP {
			parameters = t.InputSchema
		}
		out = append(out, ResponsesTool{
			Type:        "function",
			Name:        t.Name,
			Description: t.Description,
			Parameters:  parameters,
			Strict:      boolPtr(false),
		})
	}
	return out
}

func boolPtr(v bool) *bool {
	return &v
}

// isReasoningModel reports whether model is a reasoning model that does not
// support sampling parameters (temperature, top_p) via the Responses API.
// GPT-5 and every later generation are reasoning-only; the Responses API
// returns "Unsupported parameter: temperature" if these fields are present.
//
// Keyed on the generation number instead of a "gpt-5" prefix: pinning the
// prefix meant each new family (gpt-6-astra and whatever follows) silently
// fell through to the sampling branch and failed upstream on every compat
// request until someone edited this line.
func isReasoningModel(model string) bool {
	major, ok := openAIModelGeneration(model)
	return (ok && major >= 5) || openai.IsGPT6SolOrLunaModelSpelling(model)
}

// openAIModelGeneration extracts N from a "gpt-N[.M][-suffix]" model id.
// ok is false for non-GPT ids and for GPT families that carry no numeric
// generation (gpt-image-1, gpt-audio, ...).
func openAIModelGeneration(model string) (int, bool) {
	rest, ok := strings.CutPrefix(strings.ToLower(strings.TrimSpace(model)), "gpt-")
	if !ok {
		return 0, false
	}
	major, digits := 0, 0
	for _, r := range rest {
		if r < '0' || r > '9' {
			break
		}
		major = major*10 + int(r-'0')
		digits++
	}
	if digits == 0 {
		return 0, false
	}
	return major, true
}

// normalizeToolParameters ensures the tool parameter schema is valid for
// OpenAI's Responses API, which requires "properties" on object schemas.
//
//   - nil/empty → {"type":"object","properties":{}}
//   - type=object without properties → adds "properties": {}
//   - otherwise → returned unchanged
func normalizeToolParameters(schema json.RawMessage) json.RawMessage {
	if len(schema) == 0 || string(schema) == "null" {
		return json.RawMessage(`{"type":"object","properties":{}}`)
	}

	var m map[string]json.RawMessage
	if err := json.Unmarshal(schema, &m); err != nil {
		return schema
	}

	typ := m["type"]
	if string(typ) != `"object"` {
		return schema
	}

	if _, ok := m["properties"]; ok {
		return schema
	}

	m["properties"] = json.RawMessage(`{}`)
	out, err := json.Marshal(m)
	if err != nil {
		return schema
	}
	return out
}
