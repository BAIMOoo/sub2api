package service

import (
	"bytes"
	"errors"
	"fmt"
	"net/http"
	"slices"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/tidwall/gjson"
	"go.uber.org/zap"
)

type codexHTTPMode struct {
	lite      bool
	source    codexHTTPModeSource
	alignment codexHTTPAlignmentStatus
}

type codexHTTPModeSource string

const (
	codexHTTPModeSourceAccountMetadata codexHTTPModeSource = "account_metadata"
	codexHTTPModeSourceModelDefault    codexHTTPModeSource = "model_default"
	codexHTTPModeSourceRequestFallback codexHTTPModeSource = "request_fallback"
	codexHTTPModeSourceStandardDefault codexHTTPModeSource = "standard_fallback"
)

type codexHTTPAlignmentStatus string

const (
	codexHTTPAlignmentBaselineSelected codexHTTPAlignmentStatus = "baseline_selected"
	codexHTTPAlignmentUnverified       codexHTTPAlignmentStatus = "unverified"
)

// resolveCodexHTTPMode uses only local capability metadata. Selection is based
// on the final account/model and never triggers model-catalog I/O in a request.
func resolveCodexHTTPMode(c *gin.Context, account *Account, model string, body []byte) codexHTTPMode {
	model = strings.TrimSpace(model)
	if account == nil {
		return codexHTTPMode{
			source:    codexHTTPModeSourceStandardDefault,
			alignment: codexHTTPAlignmentUnverified,
		}
	}

	// SetupToken shares Codex protocol defaults with OAuth for HTTP layout.
	capabilityAccount := *account
	capabilityAccount.Type = AccountTypeOAuth
	capability := bytes.TrimSpace(accountCodexToolCapabilities(&capabilityAccount, model)["use_responses_lite"])
	if bytes.Equal(capability, []byte("true")) || bytes.Equal(capability, []byte("false")) {
		source := codexHTTPModeSourceModelDefault
		if metadata, ok := account.GetUpstreamModelMetadata(model); ok {
			persisted := bytes.TrimSpace(metadata.CodexToolCapabilities["use_responses_lite"])
			if bytes.Equal(persisted, []byte("true")) || bytes.Equal(persisted, []byte("false")) {
				source = codexHTTPModeSourceAccountMetadata
			}
		}
		return codexHTTPMode{
			lite:      bytes.Equal(capability, []byte("true")),
			source:    source,
			alignment: codexHTTPAlignmentBaselineSelected,
		}
	}

	// The pinned 0.158.0 standard capture establishes this model default.
	if model == "gpt-5.5" && len(capability) == 0 {
		return codexHTTPMode{
			source:    codexHTTPModeSourceModelDefault,
			alignment: codexHTTPAlignmentBaselineSelected,
		}
	}
	if c != nil {
		if raw, exists := c.Get(codexHTTPDeclaredModeKey); exists {
			if declaration, ok := raw.(codexHTTPDeclaredMode); ok && declaration.declared {
				return codexHTTPMode{
					lite:      declaration.lite,
					source:    codexHTTPModeSourceRequestFallback,
					alignment: codexHTTPAlignmentUnverified,
				}
			}
		}
		if declared := c.GetHeader(responsesLiteHeader); declared != "" {
			return codexHTTPMode{
				lite:      isOpenAIResponsesLiteHeader(declared),
				source:    codexHTTPModeSourceRequestFallback,
				alignment: codexHTTPAlignmentUnverified,
			}
		}
	}
	if isOpenAIResponsesLiteWebSocketPayload(body) {
		return codexHTTPMode{
			lite:      true,
			source:    codexHTTPModeSourceRequestFallback,
			alignment: codexHTTPAlignmentUnverified,
		}
	}
	return codexHTTPMode{
		source:    codexHTTPModeSourceStandardDefault,
		alignment: codexHTTPAlignmentUnverified,
	}
}

// normalizeBody is the final fixed-contract boundary shared by normal
// forwarding and passthrough. It only rewrites a private attempt body.
func (policy codexHTTPPolicy) normalizeBody(
	c *gin.Context,
	account *Account,
	body []byte,
) (normalized []byte, mode codexHTTPMode, err error) {
	if !policy.usesFixedExecContract() {
		return body, codexHTTPMode{}, nil
	}

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
		c.JSON(http.StatusBadRequest, gin.H{"error": gin.H{
			"type":    "invalid_request_error",
			"message": err.Error(),
			"param":   param,
		}})
	}()

	model := gjson.GetBytes(body, "model").String()
	mode = resolveCodexHTTPMode(c, account, model, body)
	compact := isOpenAIResponsesCompactPath(c)
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
	if _, exists := request["tool_choice"]; !exists {
		request["tool_choice"] = "auto"
	}
	if _, exists := request["parallel_tool_calls"]; !exists {
		request["parallel_tool_calls"] = true
	}
	if request["reasoning"] == nil {
		request["reasoning"] = map[string]any{}
	}
	if reasoning, ok := request["reasoning"].(map[string]any); ok {
		if _, exists := reasoning["effort"]; !exists {
			effort := ""
			if metadata, ok := account.GetUpstreamModelMetadata(model); ok {
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
	ensureCodexHTTPEncryptedContentInclude(request)
	if err := ensureCodexHTTPSession(c, account, request); err != nil {
		return nil, mode, err
	}

	if mode.lite {
		if _, err := canonicalizeCodexExecLiteRequest(request); err != nil {
			return nil, mode, err
		}
		if _, err := normalizeOpenAIResponsesLiteTools(request); err != nil {
			return nil, mode, err
		}
		ensureCodexLiteToolsCarrier(request)
	} else {
		if err := restoreCodexStandardTools(request); err != nil {
			return nil, mode, err
		}
		if request["tools"] == nil {
			request["tools"] = []any{}
		}
	}
	if compact {
		if err := setCodexHTTPCompactionTrigger(request); err != nil {
			return nil, mode, err
		}
	}
	toolNameReverse, _, err := aliasOpenAIOAuthReservedToolNames(request)
	if err != nil {
		return nil, mode, err
	}
	mergeCodexToolNameReverse(c, toolNameReverse)

	ensureCodexHTTPEncryptedContentInclude(request)
	normalized, err = marshalOpenAIUpstreamJSON(request)
	if err != nil {
		return nil, mode, fmt.Errorf("encode Codex HTTP body: %w", err)
	}
	logCodexHTTPModeSelection(account, normalized, mode)
	return normalized, mode, nil
}

func logCodexHTTPModeSelection(account *Account, body []byte, mode codexHTTPMode) {
	logger.L().Info("codex HTTP egress mode selected",
		zap.Int64("account_id", account.ID),
		zap.String("upstream_model", gjson.GetBytes(body, "model").String()),
		zap.Bool("responses_lite", mode.lite),
		zap.String("mode_source", string(mode.source)),
		zap.String("alignment_status", string(mode.alignment)),
		zap.String("codex_version", codexExecHTTPVersion),
	)
}

func (policy codexHTTPPolicy) applyModeHeaders(c *gin.Context, account *Account, req *http.Request, body []byte, mode codexHTTPMode) error {
	if !policy.usesFixedExecContract() {
		return applyMappedGPT55LiteCompatibility(req, account, body)
	}
	// The pinned auth provider sends this routing header, but the captured
	// HTTP/1.1 order doesn't cover it. Never send the request without it.
	if req.Header.Get(openAIChatGPTFedRAMPHeader) != "" {
		return &CodexHTTPTransportConfigError{reason: "FedRAMP routing is not verified for the fixed header order"}
	}
	order := tlsfingerprint.OpenAIResponsesHeaderOrder().Order
	for name := range req.Header {
		if !slices.Contains(order, strings.ToLower(name)) {
			delete(req.Header, name)
		}
	}
	req.Header.Set("Accept", "text/event-stream")
	req.Header.Set("Content-Type", "application/json")
	req.Header.Del(responsesLiteHeader)
	if mode.lite {
		req.Header.Set(responsesLiteHeader, "true")
	}
	if isOpenAIResponsesCompactPath(c) {
		ensureOpenAIRemoteCompactionV2BetaFeature(req.Header)
	}
	session := gjson.GetBytes(body, "prompt_cache_key").String()
	applyCodexSessionIdentityHeaders(req.Header, session)
	for _, header := range []string{"session-id", "thread-id", "x-client-request-id"} {
		req.Header.Set(header, session)
	}
	req.Header.Set("x-codex-window-id", gjson.GetBytes(body, "client_metadata.x-codex-window-id").String())
	req.Header.Set("x-codex-turn-metadata", gjson.GetBytes(body, "client_metadata.x-codex-turn-metadata").String())
	return nil
}

func ensureCodexHTTPEncryptedContentInclude(request map[string]any) bool {
	const encrypted = "reasoning.encrypted_content"
	switch existing := request["include"].(type) {
	case nil:
		request["include"] = []any{encrypted}
		return true
	case []any:
		for _, value := range existing {
			if text, ok := value.(string); ok && text == encrypted {
				return false
			}
		}
		request["include"] = append(existing, encrypted)
		return true
	default:
		return false
	}
}

func ensureCodexHTTPSession(c *gin.Context, account *Account, request map[string]any) error {
	metadata, _ := request["client_metadata"].(map[string]any)
	if metadata == nil {
		metadata = map[string]any{}
		request["client_metadata"] = metadata
	}
	session := firstNonEmptyString(request["prompt_cache_key"], metadata["session_id"])
	if session == "" {
		incoming := ""
		if c != nil {
			incoming = firstNonEmpty(c.GetHeader("session-id"), c.GetHeader("session_id"))
		}
		if incoming != "" {
			session = scopeCodexAccountIdentityValue(
				codexAccountIdentitySource(c, account),
				getAPIKeyIDFromContext(c),
				"session",
				incoming,
			)
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
		if err := decodeOpenAIJSONUseNumber([]byte(raw), &turn); err != nil {
			return newOpenAIResponsesLiteValidationError(
				"client_metadata",
				"invalid Codex turn metadata",
			)
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
		tools, ok := item["tools"].([]any)
		if !ok && item["tools"] != nil {
			return fmt.Errorf("codex additional_tools.tools must be an array")
		}
		moved = append(moved, tools...)
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

func ensureCodexLiteToolsCarrier(request map[string]any) {
	input, ok := request["input"].([]any)
	if !ok {
		return
	}
	for _, raw := range input {
		item, _ := raw.(map[string]any)
		if strings.TrimSpace(firstNonEmptyString(item["type"])) == openAIAdditionalToolsItemType {
			return
		}
	}
	request["input"] = append([]any{map[string]any{
		"type":  openAIAdditionalToolsItemType,
		"role":  "developer",
		"tools": []any{},
	}}, input...)
}
