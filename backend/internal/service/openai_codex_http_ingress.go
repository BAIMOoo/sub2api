package service

import (
	"fmt"
	"net/http"
	"slices"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
	"go.uber.org/zap"
)

const codexHTTPDeclaredModeKey = "codex_http_declared_mode"

type codexHTTPDeclaredMode struct {
	declared bool
	lite     bool
}

var codexHTTPIgnoredControls = []string{
	"max_tokens",
	"max_completion_tokens",
	"max_output_tokens",
	"temperature",
	"top_p",
	"frequency_penalty",
	"presence_penalty",
	"stop",
	"stop_sequences",
	"truncation",
	"n",
	"seed",
}

var codexHTTPUnsupportedFields = []string{
	"chat_template_kwargs",
	"user",
	"prompt_cache_retention",
	"safety_identifier",
	"stream_options",
}

func prepareCodexHTTPIngress(c *gin.Context, account *Account, body []byte) ([]byte, error) {
	if !codexHTTPPolicyForRequest(c, account).usesFixedExecContract() {
		return body, nil
	}
	if err := validateCodexHTTPIngressContent(body); err != nil {
		writeCodexHTTPCompatibilityError(c, err)
		return nil, err
	}
	for _, field := range codexHTTPUnsupportedFields {
		if gjson.GetBytes(body, field).Exists() {
			compatErr := newOpenAIResponsesLiteValidationError(
				"request."+field,
				"field cannot be represented by the fixed Codex HTTP contract",
			)
			writeCodexHTTPCompatibilityError(c, compatErr)
			return nil, compatErr
		}
	}
	var request map[string]any
	if err := decodeOpenAIJSONUseNumber(body, &request); err != nil {
		compatErr := fmt.Errorf("decode fixed Codex HTTP request: %w", err)
		writeCodexHTTPCompatibilityError(c, compatErr)
		return nil, compatErr
	}
	if err := validateCodexHTTPFunctionTools(request); err != nil {
		writeCodexHTTPCompatibilityError(c, err)
		return nil, err
	}

	if err := validateCodexHTTPChatHistory(body); err != nil {
		writeCodexHTTPCompatibilityError(c, err)
		return nil, err
	}

	declared := codexHTTPDeclaredMode{}
	if header := c.GetHeader(responsesLiteHeader); header != "" {
		declared = codexHTTPDeclaredMode{
			declared: true,
			lite:     isOpenAIResponsesLiteHeader(header),
		}
	} else if isOpenAIResponsesLiteWebSocketPayload(body) {
		declared = codexHTTPDeclaredMode{declared: true, lite: true}
	}
	c.Set(codexHTTPDeclaredModeKey, declared)

	var ignored []string
	for _, field := range codexHTTPIgnoredControls {
		if !gjson.GetBytes(body, field).Exists() {
			continue
		}
		var err error
		body, err = sjson.DeleteBytes(body, field)
		if err != nil {
			return nil, fmt.Errorf("remove unsupported Codex control %s: %w", field, err)
		}
		ignored = append(ignored, field)
	}
	if len(ignored) > 0 {
		logger.L().Info("codex HTTP generation controls ignored",
			zap.Int64("account_id", account.ID),
			zap.String("codex_version", codexExecHTTPVersion),
			zap.Strings("ignored_parameters", ignored),
		)
	}
	return body, nil
}

func validateCodexHTTPFunctionTools(request map[string]any) error {
	var validate func(any) error
	validate = func(raw any) error {
		if raw == nil {
			return nil
		}
		tools, ok := raw.([]any)
		if !ok {
			return newOpenAIResponsesLiteValidationError("tools", "codex tools must be an array")
		}
		for _, rawTool := range tools {
			tool, ok := rawTool.(map[string]any)
			if !ok {
				continue
			}
			if tool["type"] == "namespace" {
				if err := validate(tool["tools"]); err != nil {
					return err
				}
				if err := validate(tool["children"]); err != nil {
					return err
				}
			}
			if tool["type"] == "function" && strings.TrimSpace(firstNonEmptyString(tool["name"])) == "" {
				function, _ := tool["function"].(map[string]any)
				if strings.TrimSpace(firstNonEmptyString(function["name"])) == "" {
					return newOpenAIResponsesLiteValidationError(
						"tools",
						"function tool requires a name; it cannot be silently discarded",
					)
				}
			}
		}
		return nil
	}
	if err := validate(request["tools"]); err != nil {
		return err
	}
	input, _ := request["input"].([]any)
	callIDs := make(map[string]struct{})
	for _, raw := range input {
		item, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		typ, _ := item["type"].(string)
		if typ == "item_reference" {
			if id := strings.TrimSpace(firstNonEmptyString(item["id"])); id != "" {
				callIDs[id] = struct{}{}
			}
		}
		if isCodexToolCallInputType(typ) {
			if callID := strings.TrimSpace(firstNonEmptyString(item["call_id"], item["id"])); callID != "" {
				callIDs[callID] = struct{}{}
			}
		}
	}
	hasPreviousResponse := strings.TrimSpace(firstNonEmptyString(request["previous_response_id"])) != ""
	for _, raw := range input {
		item, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		typ, _ := item["type"].(string)
		if codexInputItemRequiresName(typ) &&
			strings.TrimSpace(firstNonEmptyString(item["name"], item["tool_name"])) == "" {
			function, _ := item["function"].(map[string]any)
			if strings.TrimSpace(firstNonEmptyString(function["name"])) == "" {
				return newOpenAIResponsesLiteValidationError(
					"input",
					"tool call requires its original name; a replacement name cannot be inferred",
				)
			}
		}
		if item["role"] == "tool" {
			if strings.TrimSpace(firstNonEmptyString(item["call_id"], item["tool_call_id"], item["id"])) == "" {
				return newOpenAIResponsesLiteValidationError(
					"input",
					"tool result requires a call identifier",
				)
			}
			if _, lossless := extractLosslessTextFromContent(item["content"]); !lossless {
				return newOpenAIResponsesLiteValidationError(
					"input",
					"legacy tool message content cannot be losslessly converted; use function_call_output",
				)
			}
		}
		if err := validateCodexHTTPContentText(item["content"]); err != nil {
			return err
		}
		if item["type"] == openAIAdditionalToolsItemType {
			for field := range item {
				switch field {
				case "type", "id", "role", "tools":
				default:
					return newOpenAIResponsesLiteValidationError(
						"input",
						"additional_tools carrier contains fields that cannot be preserved during layout conversion",
					)
				}
			}
			if err := validate(item["tools"]); err != nil {
				return err
			}
		}
		outputType := false
		switch typ {
		case "function_call_output", "custom_tool_call_output", "mcp_tool_call_output", "tool_search_output":
			outputType = true
		}
		if item["role"] == "tool" {
			outputType = true
		}
		if outputType {
			callID := strings.TrimSpace(firstNonEmptyString(item["call_id"], item["tool_call_id"], item["id"]))
			if callID == "" {
				if typ == "function_call_output" &&
					strings.TrimSpace(firstNonEmptyString(item["name"])) != "" {
					continue
				}
				return newOpenAIResponsesLiteValidationError("input", "tool result requires a call identifier")
			}
		}
		if outputType && !hasPreviousResponse {
			callID := strings.TrimSpace(firstNonEmptyString(item["call_id"], item["tool_call_id"], item["id"]))
			if callID != "" {
				if _, ok := callIDs[callID]; !ok {
					return newOpenAIResponsesLiteValidationError(
						"input",
						"tool result references a call that is absent from this request and no previous response is declared",
					)
				}
			}
		}
	}
	return nil
}

func validateCodexHTTPContentText(raw any) error {
	parts, ok := raw.([]any)
	if !ok {
		return nil
	}
	for _, rawPart := range parts {
		part, ok := rawPart.(map[string]any)
		if !ok {
			continue
		}
		if text, exists := part["text"]; exists {
			if _, ok := text.(string); !ok {
				return newOpenAIResponsesLiteValidationError(
					"input",
					"message content text must be a string for lossless Codex conversion",
				)
			}
		}
	}
	return nil
}

func validateCodexHTTPChatHistory(body []byte) error {
	if !gjson.GetBytes(body, "messages").Exists() {
		return nil
	}
	allowed := "model messages instructions prompt_cache_options prompt_cache_key stream stream_options tools parallel_tool_calls tool_choice reasoning_effort service_tier response_format functions function_call metadata user"
	if err := validateCodexHTTPFieldSet(gjson.ParseBytes(body), "request", allowed, true); err != nil {
		return err
	}
	for index, message := range gjson.GetBytes(body, "messages").Array() {
		var invalid string
		message.ForEach(func(key, _ gjson.Result) bool {
			switch key.String() {
			case "role", "content", "refusal", "reasoning_content", "reasoning", "name", "tool_calls", "tool_call_id", "function_call":
				return true
			default:
				invalid = key.String()
				return false
			}
		})
		if invalid != "" {
			return newOpenAIResponsesLiteValidationError(
				fmt.Sprintf("messages[%d].%s", index, invalid),
				"chat history contains a field that cannot be losslessly converted",
			)
		}
		for callIndex, call := range message.Get("tool_calls").Array() {
			path := fmt.Sprintf("messages[%d].tool_calls[%d]", index, callIndex)
			if err := validateCodexHTTPFieldSet(call, path, "id type index function", false); err != nil {
				return err
			}
			if err := validateCodexHTTPFieldSet(call.Get("function"), path+".function", "name arguments", false); err != nil {
				return err
			}
		}
		if err := validateCodexHTTPFieldSet(
			message.Get("function_call"),
			fmt.Sprintf("messages[%d].function_call", index),
			"name arguments",
			false,
		); err != nil {
			return err
		}
	}
	for index, tool := range gjson.GetBytes(body, "tools").Array() {
		path := fmt.Sprintf("tools[%d]", index)
		if err := validateCodexHTTPFieldSet(tool, path, "type function", false); err != nil {
			return err
		}
		if err := validateCodexHTTPFieldSet(tool.Get("function"), path+".function", "name description parameters strict", false); err != nil {
			return err
		}
	}
	for index, function := range gjson.GetBytes(body, "functions").Array() {
		if err := validateCodexHTTPFieldSet(function, fmt.Sprintf("functions[%d]", index), "name description parameters strict", false); err != nil {
			return err
		}
	}
	return nil
}

func validateCodexHTTPFieldSet(value gjson.Result, path, allowed string, controls bool) error {
	if !value.Exists() || value.Type == gjson.Null {
		return nil
	}
	if !value.IsObject() {
		return newOpenAIResponsesLiteValidationError(path, "expected an object for lossless Codex conversion")
	}
	if controls {
		allowed += " " + strings.Join(codexHTTPIgnoredControls, " ")
	}
	allowedFields := strings.Fields(allowed)
	var invalid string
	value.ForEach(func(key, _ gjson.Result) bool {
		if !slices.Contains(allowedFields, key.String()) {
			invalid = key.String()
			return false
		}
		return true
	})
	if invalid != "" {
		return newOpenAIResponsesLiteValidationError(
			path+"."+invalid,
			"field cannot be losslessly converted to the fixed Codex contract",
		)
	}
	return nil
}

func writeCodexHTTPCompatibilityError(c *gin.Context, err error) {
	param := "input"
	if validationErr, ok := err.(*openAIResponsesLiteValidationError); ok {
		param = validationErr.param
	}
	if c.Request != nil && strings.HasSuffix(c.Request.URL.Path, "/messages") {
		writeAnthropicError(c, http.StatusBadRequest, "invalid_request_error", err.Error())
		return
	}
	c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{
		"type":    "invalid_request_error",
		"message": err.Error(),
		"param":   param,
	}})
}

func validateCodexHTTPIngressContent(body []byte) error {
	if gjson.GetBytes(body, "commands").Exists() {
		return newOpenAIResponsesLiteValidationError(
			"commands",
			"commands cannot be losslessly represented by the Codex HTTP contract",
		)
	}
	prompt := gjson.GetBytes(body, "prompt")
	if prompt.Exists() && prompt.Type != gjson.Null {
		if prompt.Type != gjson.String ||
			gjson.GetBytes(body, "input").Exists() ||
			gjson.GetBytes(body, "messages").Exists() {
			return newOpenAIResponsesLiteValidationError(
				"prompt",
				"prompt is unsupported or conflicts with an explicit conversation",
			)
		}
	}
	if gjson.GetBytes(body, "input").Exists() && gjson.GetBytes(body, "messages").Exists() {
		return newOpenAIResponsesLiteValidationError(
			"messages",
			"input and messages cannot both define the conversation",
		)
	}
	if gjson.GetBytes(body, "functions").Exists() && gjson.GetBytes(body, "tools").Exists() {
		return newOpenAIResponsesLiteValidationError(
			"tools",
			"tools and legacy functions cannot both define the tool set",
		)
	}
	if openAIRequestBodyMayContainEmptyBase64InputImage(body) {
		return newOpenAIResponsesLiteValidationError(
			"input",
			"empty image input cannot be forwarded without losing content",
		)
	}
	return nil
}
