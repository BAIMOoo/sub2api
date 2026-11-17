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

// These are generation controls the pinned Codex endpoint cannot honor. Log
// their names before typed Chat/Messages conversion can silently discard them.
// Task instructions, history, tool declarations and results are never in this list.
var codexHTTPIgnoredControls = []string{
	"max_tokens", "max_completion_tokens", "max_output_tokens", "temperature", "top_p",
	"frequency_penalty", "presence_penalty", "stop", "stop_sequences", "truncation", "n", "seed",
}

func prepareCodexHTTPIngress(c *gin.Context, account *Account, body []byte) ([]byte, error) {
	if !codexHTTPPolicyForRequest(c, account).preservesCodexExec() {
		return body, nil
	}
	if err := validateCodexHTTPIngressContent(body); err != nil {
		writeCodexHTTPCompatibilityError(c, err)
		return nil, err
	}
	if strings.HasSuffix(c.Request.URL.Path, "/messages") {
		if err := validateCodexHTTPFieldSet(gjson.ParseBytes(body), "request", "model system messages tools tool_choice thinking output_config metadata stream service_tier", true); err != nil {
			writeCodexHTTPCompatibilityError(c, err)
			return nil, err
		}
		for field, allowed := range map[string]string{"thinking": "type budget_tokens", "output_config": "effort"} {
			if err := validateCodexHTTPFieldSet(gjson.GetBytes(body, field), field, allowed, false); err != nil {
				writeCodexHTTPCompatibilityError(c, err)
				return nil, err
			}
		}
		for i, message := range gjson.GetBytes(body, "messages").Array() {
			if err := validateCodexHTTPFieldSet(message, fmt.Sprintf("messages[%d]", i), "role content", false); err != nil {
				writeCodexHTTPCompatibilityError(c, err)
				return nil, err
			}
		}
	} else {
		if err := validateCodexHTTPChatHistory(body); err != nil {
			writeCodexHTTPCompatibilityError(c, err)
			return nil, err
		}
	}
	declared := codexHTTPDeclaredMode{}
	if header := c.GetHeader(responsesLiteHeader); header != "" {
		declared = codexHTTPDeclaredMode{declared: true, lite: isOpenAIResponsesLiteHeader(header)}
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

// Typed Chat decoding drops unknown fields. Reject unsupported task data before
// that happens; content parts themselves stay RawMessage for strict conversion.
func validateCodexHTTPChatHistory(body []byte) error {
	if !gjson.GetBytes(body, "messages").Exists() {
		return nil
	}
	if err := validateCodexHTTPFieldSet(gjson.ParseBytes(body), "request", "model messages instructions prompt_cache_options prompt_cache_key stream stream_options tools parallel_tool_calls tool_choice reasoning_effort service_tier response_format functions function_call metadata user", true); err != nil {
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
			return newOpenAIResponsesLiteValidationError(fmt.Sprintf("messages[%d].%s", index, invalid), "chat history contains a field that cannot be losslessly converted")
		}
		for i, call := range message.Get("tool_calls").Array() {
			path := fmt.Sprintf("messages[%d].tool_calls[%d]", index, i)
			if err := validateCodexHTTPFieldSet(call, path, "id type index function", false); err != nil {
				return err
			}
			if err := validateCodexHTTPFieldSet(call.Get("function"), path+".function", "name arguments", false); err != nil {
				return err
			}
		}
		if err := validateCodexHTTPFieldSet(message.Get("function_call"), fmt.Sprintf("messages[%d].function_call", index), "name arguments", false); err != nil {
			return err
		}
	}
	for i, tool := range gjson.GetBytes(body, "tools").Array() {
		path := fmt.Sprintf("tools[%d]", i)
		if err := validateCodexHTTPFieldSet(tool, path, "type function", false); err != nil {
			return err
		}
		if err := validateCodexHTTPFieldSet(tool.Get("function"), path+".function", "name description parameters strict", false); err != nil {
			return err
		}
	}
	for i, function := range gjson.GetBytes(body, "functions").Array() {
		if err := validateCodexHTTPFieldSet(function, fmt.Sprintf("functions[%d]", i), "name description parameters strict", false); err != nil {
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
		return newOpenAIResponsesLiteValidationError(path+"."+invalid, "field cannot be losslessly converted to the fixed Codex contract")
	}
	return nil
}

func writeCodexHTTPCompatibilityError(c *gin.Context, err error) {
	param := "input"
	if typed, ok := err.(*openAIResponsesLiteValidationError); ok {
		param = typed.param
	}
	if strings.HasSuffix(c.Request.URL.Path, "/messages") {
		writeAnthropicError(c, http.StatusBadRequest, "invalid_request_error", err.Error())
		return
	}
	c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{
		"type": "invalid_request_error", "message": err.Error(), "param": param,
	}})
}

// Reject ambiguous legacy content before compatibility helpers can silently
// discard it. Native Responses items otherwise retain their unknown fields for
// upstream validation; this is not an exhaustive model-feature allowlist.
func validateCodexHTTPIngressContent(body []byte) error {
	if gjson.GetBytes(body, "commands").Exists() {
		return newOpenAIResponsesLiteValidationError("commands", "commands cannot be losslessly represented by the Codex HTTP contract")
	}
	prompt := gjson.GetBytes(body, "prompt")
	if prompt.Exists() && prompt.Type != gjson.Null {
		if prompt.Type != gjson.String || gjson.GetBytes(body, "input").Exists() || gjson.GetBytes(body, "messages").Exists() {
			return newOpenAIResponsesLiteValidationError("prompt", "prompt is unsupported or conflicts with an explicit conversation")
		}
	}
	if gjson.GetBytes(body, "input").Exists() && gjson.GetBytes(body, "messages").Exists() {
		return newOpenAIResponsesLiteValidationError("messages", "input and messages cannot both define the conversation")
	}
	if gjson.GetBytes(body, "functions").Exists() && gjson.GetBytes(body, "tools").Exists() {
		return newOpenAIResponsesLiteValidationError("tools", "tools and legacy functions cannot both define the tool set")
	}
	if openAIRequestBodyMayContainEmptyBase64InputImage(body) {
		return newOpenAIResponsesLiteValidationError("input", "empty image input cannot be forwarded without losing content")
	}
	return nil
}
