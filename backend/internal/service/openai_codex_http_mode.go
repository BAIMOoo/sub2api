package service

import (
	"bytes"
	"fmt"
	"net/http"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/gin-gonic/gin"
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

// normalizeCodexHTTPModeBody is the mode-specific final layout step shared by
// normal forwarding and passthrough. It only rewrites a private attempt body.
func normalizeCodexHTTPModeBody(c *gin.Context, account *Account, body []byte) ([]byte, codexHTTPMode, error) {
	if !codexHTTPPolicyForRequest(c, account).usesFixedExecContract() {
		return body, codexHTTPMode{}, nil
	}

	mode := resolveCodexHTTPMode(c, account, gjson.GetBytes(body, "model").String(), body)
	if isOpenAIResponsesCompactPath(c) {
		logCodexHTTPModeSelection(account, body, mode)
		return body, mode, nil
	}
	var request map[string]any
	if err := decodeOpenAIJSONUseNumber(body, &request); err != nil {
		return nil, mode, fmt.Errorf("decode Codex HTTP body: %w", err)
	}
	normalizeOpenAIResponsesStringInput(request)

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
	toolNameReverse, _, err := aliasOpenAIOAuthReservedToolNames(request)
	if err != nil {
		return nil, mode, err
	}
	mergeCodexToolNameReverse(c, toolNameReverse)

	normalized, err := marshalOpenAIUpstreamJSON(request)
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

func applyCodexHTTPMode(c *gin.Context, account *Account, req *http.Request, body []byte, mode codexHTTPMode) error {
	if !codexHTTPPolicyForRequest(c, account).usesFixedExecContract() {
		return applyMappedGPT55LiteCompatibility(req, account, body)
	}
	req.Header.Del(responsesLiteHeader)
	if mode.lite {
		req.Header.Set(responsesLiteHeader, "true")
	}
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
