package service

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
	"go.uber.org/zap"
)

type codexHTTPMode struct {
	lite     bool
	source   string
	verified bool
}

// The legacy client endpoint is a unary adapter onto the fixed client's V2
// compaction request, which streams through /responses. Preserve the complete
// opaque compaction output and only adapt the outer response object.
func normalizeCodexHTTPCompactResponse(c *gin.Context, account *Account, body []byte) ([]byte, error) {
	if !isOpenAIResponsesCompactPath(c) || !codexHTTPPolicyForRequest(c, account).preservesCodexExec() {
		return body, nil
	}
	if !responsesOutputHasCompactionItem(body) {
		return nil, fmt.Errorf("codex compaction response is missing its compaction output")
	}
	return sjson.SetBytes(body, "object", "response.compaction")
}

// Resolve against the final account/model. Model catalog I/O belongs to the
// existing metadata synchronization path, never to an inference request.
func resolveCodexHTTPMode(c *gin.Context, account *Account, model string, body []byte) codexHTTPMode {
	model = strings.TrimSpace(model)
	// Setup tokens share the Codex protocol defaults. Limit this view to HTTP
	// mode selection so the existing catalog/WS capability behavior is unchanged.
	capabilityAccount := *account
	capabilityAccount.Type = AccountTypeOAuth
	capability := bytes.TrimSpace(accountCodexToolCapabilities(&capabilityAccount, model)["use_responses_lite"])
	if bytes.Equal(capability, []byte("true")) || bytes.Equal(capability, []byte("false")) {
		source := "model_default"
		if metadata, ok := account.GetUpstreamModelMetadata(model); ok {
			if persisted := bytes.TrimSpace(metadata.CodexToolCapabilities["use_responses_lite"]); bytes.Equal(persisted, []byte("true")) || bytes.Equal(persisted, []byte("false")) {
				source = "account_metadata"
			}
		}
		return codexHTTPMode{lite: bytes.Equal(capability, []byte("true")), source: source, verified: true}
	}
	// The existing GPT-5.5 compatibility rule and captured standard fixture
	// establish this model's default. Explicit account capabilities win above.
	if model == "gpt-5.5" && len(capability) == 0 {
		return codexHTTPMode{source: "model_default", verified: true}
	}
	if raw, exists := c.Get(codexHTTPDeclaredModeKey); exists {
		if declaration, ok := raw.(codexHTTPDeclaredMode); ok && declaration.declared {
			return codexHTTPMode{lite: declaration.lite, source: "request_fallback"}
		}
		return codexHTTPMode{source: "standard_fallback"}
	}
	if declared := c.GetHeader(responsesLiteHeader); declared != "" {
		return codexHTTPMode{
			lite:   isOpenAIResponsesLiteHeader(declared),
			source: "request_fallback",
		}
	}
	if isOpenAIResponsesLiteWebSocketPayload(body) {
		return codexHTTPMode{lite: true, source: "request_fallback"}
	}
	return codexHTTPMode{source: "standard_fallback"}
}

// normalizeCodexHTTPBody is the common final HTTP boundary for Responses,
// passthrough, Chat and Messages. It only rewrites a private decoded body, so
// account failover cannot inherit a previous account's mode selection.
func normalizeCodexHTTPBody(c *gin.Context, account *Account, body []byte) (normalized []byte, mode codexHTTPMode, err error) {
	if !codexHTTPPolicyForRequest(c, account).preservesCodexExec() {
		return body, codexHTTPMode{}, nil
	}
	// These failures describe the caller's request, not an upstream outage.
	// Write once here so every HTTP entrypoint retains a 400 instead of falling
	// through to its generic transport-error response after request building.
	defer func() {
		if err == nil {
			return
		}
		param := "tools"
		var validationErr *openAIResponsesLiteValidationError
		if errors.As(err, &validationErr) {
			param = validationErr.param
		}
		setOpsUpstreamError(c, http.StatusBadRequest, err.Error(), "")
		if strings.HasSuffix(c.Request.URL.Path, "/messages") {
			writeAnthropicError(c, http.StatusBadRequest, "invalid_request_error", err.Error())
			return
		}
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{
			"type": "invalid_request_error", "message": err.Error(), "param": param,
		}})
	}()
	model := gjson.GetBytes(body, "model").String()
	mode = resolveCodexHTTPMode(c, account, model, body)
	var request map[string]any
	if err := decodeOpenAIJSONUseNumber(body, &request); err != nil {
		return nil, mode, fmt.Errorf("decode Codex HTTP body: %w", err)
	}
	for _, field := range codexHTTPIgnoredControls {
		delete(request, field)
	}
	if metadata, ok := request["client_metadata"].(map[string]any); ok {
		delete(metadata, responsesLiteWSMetadataKey)
	}
	if err := validateCodexHTTPFunctionTools(request); err != nil {
		return nil, mode, err
	}
	normalizeOpenAIResponsesStringInput(request)
	normalizeCodexSystemRoleMessagesInInput(request)
	request["store"], request["stream"] = false, true
	if _, ok := request["tool_choice"]; !ok {
		request["tool_choice"] = "auto"
	}
	if _, ok := request["parallel_tool_calls"]; !ok {
		request["parallel_tool_calls"] = true
	}
	if request["reasoning"] == nil {
		request["reasoning"] = map[string]any{}
	}
	if reasoning, ok := request["reasoning"].(map[string]any); ok {
		if _, exists := reasoning["effort"]; !exists {
			effort := ""
			if metadata, exists := account.GetUpstreamModelMetadata(model); exists {
				effort = metadata.DefaultReasoningLevel
			}
			if effort == "" {
				switch model {
				case "gpt-5.5":
					effort = "medium"
				case "gpt-6-astra":
					effort = "low"
				}
			}
			if effort != "" {
				reasoning["effort"] = effort
			}
		}
	}
	if model == "gpt-5.5" || model == "gpt-6-astra" {
		if request["text"] == nil {
			request["text"] = map[string]any{}
		}
		if text, ok := request["text"].(map[string]any); ok && text["verbosity"] == nil {
			text["verbosity"] = "low"
		}
	}
	ensureCodexEncryptedContentInclude(request)
	if err := ensureCodexHTTPSession(c, account, request); err != nil {
		return nil, mode, err
	}
	if instructions, ok := request["instructions"].(string); ok && instructions == "" {
		delete(request, "instructions")
	}
	if mode.lite {
		// Move hosted tools into the Lite carrier before validating the remaining
		// envelope. This preserves caller tools instead of rejecting a valid
		// standard-mode declaration merely because the server selected Lite.
		if _, err := canonicalizeCodexExecLiteRequest(request); err != nil {
			return nil, mode, err
		}
		if _, err := normalizeOpenAIResponsesLiteTools(request); err != nil {
			return nil, mode, err
		}
		if input, ok := request["input"].([]any); ok {
			if len(input) == 0 || firstNonEmptyString(inputItemMap(input[0])["type"]) != openAIAdditionalToolsItemType {
				request["input"] = append([]any{map[string]any{"type": openAIAdditionalToolsItemType, "role": "developer", "tools": []any{}}}, input...)
			}
		}
		ensureCodexEncryptedContentInclude(request)
	} else {
		if err := restoreCodexStandardTools(request); err != nil {
			return nil, mode, err
		}
		if request["tools"] == nil {
			request["tools"] = []any{}
		}
	}
	if isOpenAIResponsesCompactPath(c) {
		input, ok := request["input"].([]any)
		if !ok {
			return nil, mode, newOpenAIResponsesLiteValidationError("input", "compact requires a conversation")
		}
		var compactInput []any
		for _, raw := range input {
			item, _ := raw.(map[string]any)
			if item["type"] != "compaction_trigger" {
				compactInput = append(compactInput, raw)
			}
		}
		request["input"] = append(compactInput, map[string]any{"type": "compaction_trigger"})
	}
	encoded, err := marshalOpenAIUpstreamJSON(request)
	if err != nil {
		return nil, mode, err
	}
	status := "baseline_selected"
	if !mode.verified {
		status = "unverified"
	}
	logger.L().Info("codex HTTP egress mode selected",
		zap.Int64("account_id", account.ID),
		zap.String("upstream_model", model),
		zap.Bool("responses_lite", mode.lite),
		zap.String("mode_source", mode.source),
		zap.String("alignment_status", status),
		zap.String("codex_version", codexExecHTTPVersion),
	)
	return encoded, mode, nil
}

func inputItemMap(raw any) map[string]any {
	item, _ := raw.(map[string]any)
	return item
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
				continue // Custom shorthand remains supported by the existing converter.
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
					return newOpenAIResponsesLiteValidationError("tools", "function tool requires a name; it cannot be silently discarded")
				}
			}
		}
		return nil
	}
	if err := validate(request["tools"]); err != nil {
		return err
	}
	input, _ := request["input"].([]any)
	for _, raw := range input {
		item, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		typ, _ := item["type"].(string)
		if codexInputItemRequiresName(typ) && strings.TrimSpace(firstNonEmptyString(item["name"], item["tool_name"])) == "" {
			function, _ := item["function"].(map[string]any)
			if strings.TrimSpace(firstNonEmptyString(function["name"])) == "" {
				return newOpenAIResponsesLiteValidationError("input", "tool call requires its original name; a replacement name cannot be inferred")
			}
		}
		if item["role"] == "tool" {
			if strings.TrimSpace(firstNonEmptyString(item["call_id"], item["tool_call_id"], item["id"])) == "" {
				return newOpenAIResponsesLiteValidationError("input", "tool result requires a call identifier")
			}
			if _, lossless := extractLosslessTextFromContent(item["content"]); !lossless {
				return newOpenAIResponsesLiteValidationError("input", "legacy tool message content cannot be losslessly converted; use function_call_output")
			}
		}
		if ok && item["type"] == openAIAdditionalToolsItemType {
			for field := range item {
				switch field {
				case "type", "id", "role", "tools":
				default:
					return newOpenAIResponsesLiteValidationError("input", "additional_tools carrier contains fields that cannot be preserved during layout conversion")
				}
			}
			if err := validate(item["tools"]); err != nil {
				return err
			}
		}
	}
	return nil
}

func finalizeCodexHTTPRequest(c *gin.Context, account *Account, req *http.Request, body []byte, mode codexHTTPMode) error {
	if err := applyCodexHTTPMode(c, account, req, body, mode); err != nil {
		return err
	}
	if codexHTTPPolicyForRequest(c, account).preservesCodexExec() {
		req.Header.Del("Content-Encoding")
		if !applyCodexRequestBodyZstd(account, req) {
			return fmt.Errorf("codex HTTP request compression failed")
		}
	} else {
		applyCodexRequestBodyZstd(account, req)
	}
	return nil
}

func applyCodexHTTPMode(c *gin.Context, account *Account, req *http.Request, body []byte, mode codexHTTPMode) error {
	if !codexHTTPPolicyForRequest(c, account).preservesCodexExec() {
		return applyMappedGPT55LiteCompatibility(req, account, body)
	}
	req.Header.Del(responsesLiteHeader)
	ensureOpenAIRemoteCompactionV2BetaFeature(req.Header)
	if mode.lite {
		req.Header.Set(responsesLiteHeader, "true")
	}
	// These body identities are already scoped by the forwarding pipeline.
	// Rebuild the final headers from that same object, without re-scoping it.
	session := gjson.GetBytes(body, "prompt_cache_key").String()
	applyCodexSessionIdentityHeaders(req.Header, session)
	for _, header := range []string{"session-id", "thread-id", "x-client-request-id"} {
		req.Header.Set(header, session)
	}
	req.Header.Set("x-codex-window-id", gjson.GetBytes(body, "client_metadata.x-codex-window-id").String())
	req.Header.Set("x-codex-turn-metadata", gjson.GetBytes(body, "client_metadata.x-codex-turn-metadata").String())
	return nil
}

func ensureCodexHTTPSession(c *gin.Context, account *Account, request map[string]any) error {
	metadata, _ := request["client_metadata"].(map[string]any)
	if metadata == nil {
		metadata = map[string]any{}
		request["client_metadata"] = metadata
	}
	session := firstNonEmptyString(request["prompt_cache_key"], metadata["session_id"])
	if session == "" {
		if incoming := firstNonEmpty(c.GetHeader("session-id"), c.GetHeader("session_id")); incoming != "" {
			session = scopeCodexAccountIdentityValue(codexAccountIdentitySource(c, account), getAPIKeyIDFromContext(c), "session", incoming)
		} else {
			session = uuid.Must(uuid.NewV7()).String()
		}
	}
	session = normalizeCodexHTTPIdentityID(session)
	request["prompt_cache_key"] = session
	metadata["session_id"], metadata["thread_id"] = session, session
	suffix := "0"
	if _, value, ok := strings.Cut(firstNonEmptyString(metadata["x-codex-window-id"]), ":"); ok && value != "" {
		suffix = value
	}
	metadata["x-codex-window-id"] = session + ":" + suffix
	turn := map[string]any{}
	if raw, ok := metadata["x-codex-turn-metadata"].(string); ok && raw != "" {
		if err := json.Unmarshal([]byte(raw), &turn); err != nil {
			return newOpenAIResponsesLiteValidationError("client_metadata", "invalid Codex turn metadata")
		}
	}
	if turn == nil {
		turn = map[string]any{}
	}
	turn["session_id"], turn["thread_id"] = session, session
	turn["window_id"] = metadata["x-codex-window-id"]
	if isOpenAIResponsesCompactPath(c) {
		turn["request_kind"] = "compaction"
	}
	turnID := firstNonEmptyString(metadata["turn_id"], turn["turn_id"])
	if turnID == "" {
		turnID = uuid.Must(uuid.NewV7()).String()
	}
	turnID = normalizeCodexHTTPIdentityID(turnID)
	metadata["turn_id"], metadata["root_turn_id"] = turnID, turnID
	turn["turn_id"], turn["root_turn_id"] = turnID, turnID
	// Preserve caller-provided protocol metadata. For absent environment hints,
	// use the pinned capture environment without adding any task instructions.
	if turn["sandbox"] == nil {
		turn["sandbox"] = "seccomp"
	}
	if turn["sandbox_mode"] == nil {
		turn["sandbox_mode"] = "read-only"
	}
	for _, field := range codexTurnMetadataClientOnlyFields {
		delete(turn, field)
	}
	encoded, err := marshalCodexTurnMetadata(turn)
	if err != nil {
		return err
	}
	metadata["x-codex-turn-metadata"] = string(encoded)
	return nil
}

// Standard Responses keeps declarations in top-level tools. Preserve namespace
// objects and every history item when converting a caller's Lite layout.
func restoreCodexStandardTools(request map[string]any) error {
	input, ok := request["input"].([]any)
	if !ok {
		return nil
	}
	var moved []any
	filtered := make([]any, 0, len(input))
	found := false
	for _, raw := range input {
		item, ok := raw.(map[string]any)
		if !ok || item["type"] != openAIAdditionalToolsItemType {
			filtered = append(filtered, raw)
			continue
		}
		found = true
		if rawTools := item["tools"]; rawTools != nil {
			tools, ok := rawTools.([]any)
			if !ok {
				return fmt.Errorf("codex additional_tools.tools must be an array")
			}
			moved = append(moved, tools...)
		}
	}
	if !found {
		return nil
	}
	existing, ok := request["tools"].([]any)
	if !ok && request["tools"] != nil {
		return fmt.Errorf("codex tools must be an array")
	}
	merged, err := mergeOpenAIResponsesLiteAdditionalTools(existing, moved)
	if err != nil {
		return err
	}
	request["tools"] = merged
	request["input"] = filtered
	return nil
}
