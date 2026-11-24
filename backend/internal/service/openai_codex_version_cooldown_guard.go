package service

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	"github.com/tidwall/gjson"
)

// codexPinnedEgressVersion returns the active Pinned Egress Version. Kept as a
// package-private function variable so unit tests can deterministically exercise
// version-lag protection against bundled model manifests without modifying the
// production constant.
var codexPinnedEgressVersion = func() string {
	return codexExecHTTPVersion
}

type codexFixedHTTPEgressCtxKey struct{}

func withCodexFixedHTTPEgress(ctx context.Context) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(ctx, codexFixedHTTPEgressCtxKey{}, true)
}

func codexFixedHTTPEgressFromContext(ctx context.Context) bool {
	if ctx == nil {
		return false
	}
	fixed, _ := ctx.Value(codexFixedHTTPEgressCtxKey{}).(bool)
	return fixed
}

func (policy codexHTTPPolicy) withEgressErrorContext(ctx context.Context) context.Context {
	if !policy.usesFixedExecContract() {
		return ctx
	}
	return withCodexFixedHTTPEgress(ctx)
}

var openAICodexClientVersionMismatchExactPhrases = []string{
	"minimal client version",
	"minimum client version",
	"unsupported client version",
	"client version is not supported",
	"client version is too old",
	"client version too old",
	"outdated client",
	"outdated codex",
	"upgrade codex",
	"update codex",
	"upgrade your codex",
	"update your codex",
	"newer version of codex",
	"newer codex client",
}

var openAICodexClientVersionQualifiers = []string{
	"upgrade",
	"update",
	"older",
	"too old",
	"below",
	"unsupported",
	"not supported",
	"required",
	"requires",
	"minimum",
	"minimal",
	"outdated",
	"at least",
}

func extractCodexClientVersionErrorText(body []byte) string {
	if len(body) == 0 {
		return ""
	}
	if !gjson.ValidBytes(body) {
		return normalizeModelNotFoundBody(body)
	}
	var parts []string
	for _, path := range []string{"detail", "error.message", "response.error.message", "message"} {
		val := gjson.GetBytes(body, path)
		if val.Type == gjson.String {
			if trimmed := strings.TrimSpace(val.String()); trimmed != "" {
				parts = append(parts, trimmed)
			}
		}
	}
	if len(parts) == 0 {
		return ""
	}
	return normalizeModelNotFoundBody([]byte(strings.Join(parts, " ")))
}

// isOpenAICodexClientVersionMismatchError reports whether an upstream HTTP 400
// explicitly rejects the request because the Codex client version is outdated
// or below the server's required minimum. Such failures are global to the
// gateway's outbound client version rather than account-specific, so they must
// not trigger 30-minute per-model cooldowns or pool failover.
func isOpenAICodexClientVersionMismatchError(statusCode int, body []byte) bool {
	if statusCode != http.StatusBadRequest {
		return false
	}
	normalized := extractCodexClientVersionErrorText(body)
	if normalized == "" {
		return false
	}
	for _, phrase := range openAICodexClientVersionMismatchExactPhrases {
		if strings.Contains(normalized, phrase) {
			return true
		}
	}
	if strings.Contains(normalized, "client version") {
		for _, qualifier := range openAICodexClientVersionQualifiers {
			if strings.Contains(normalized, qualifier) {
				return true
			}
		}
	}
	return false
}

func codexVersionLagsRequirement(pinnedVersion, requiredVersion string) bool {
	pinned := NormalizeCodexClientVersion(pinnedVersion)
	required := NormalizeCodexClientVersion(requiredVersion)
	if pinned == "" || required == "" {
		return false
	}
	return CompareVersions(pinned, required) < 0
}

func codexAccountMetadataMinimalClientVersion(account *Account, model string) string {
	if account == nil || strings.TrimSpace(model) == "" {
		return ""
	}
	metadata, ok := account.GetUpstreamModelMetadata(model)
	if !ok {
		return ""
	}
	if v := NormalizeCodexClientVersion(metadata.MinimalClientVersion); v != "" {
		return v
	}
	if raw, exists := metadata.CodexToolCapabilities["minimal_client_version"]; exists && len(raw) > 0 {
		var text string
		if json.Unmarshal(raw, &text) == nil {
			if v := NormalizeCodexClientVersion(text); v != "" {
				return v
			}
		}
	}
	return ""
}

func appendCodexModelLookupCandidates(candidates []string, seen map[string]struct{}, raw string) []string {
	add := func(value string) {
		trimmed := strings.TrimSpace(value)
		if trimmed == "" {
			return
		}
		if _, exists := seen[trimmed]; exists {
			return
		}
		seen[trimmed] = struct{}{}
		candidates = append(candidates, trimmed)
	}

	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return candidates
	}
	add(trimmed)

	segment := lastOpenAIModelSegment(trimmed)
	add(segment)

	if canonical := canonicalizeOpenAIModelAliasSpelling(segment); canonical != "" {
		add(canonical)
		segment = canonical
	}
	if normalized := normalizeCodexModel(trimmed); normalized != "" {
		add(normalized)
	}
	// Strip known effort/compact suffixes (e.g. "gpt-6.9-future-high" -> "gpt-6.9-future")
	// so unmapped effort aliases still resolve account-synced MinimalClientVersion.
	if idx := strings.LastIndex(segment, "-"); idx > 0 && idx < len(segment)-1 {
		suffix := strings.ToLower(segment[idx+1:])
		if isKnownCodexModelSuffix(suffix) {
			add(segment[:idx])
		}
	}
	return candidates
}

// codexModelMinimalClientVersion returns the highest known Model Minimal Client
// Version for the requested or mapped model across account-synced metadata and
// bundled Codex model descriptors, along with its source label.
func codexModelMinimalClientVersion(account *Account, requestedModel, modelKey string) (version, source string) {
	seen := make(map[string]struct{}, 8)
	candidates := make([]string, 0, 8)
	candidates = appendCodexModelLookupCandidates(candidates, seen, requestedModel)
	candidates = appendCodexModelLookupCandidates(candidates, seen, modelKey)

	for _, model := range candidates {
		if v := codexAccountMetadataMinimalClientVersion(account, model); v != "" {
			if version == "" || CompareVersions(v, version) > 0 {
				version, source = v, "account_metadata"
			}
		}
		if v := NormalizeCodexClientVersion(openai.BundledCodexMinimalClientVersion(model)); v != "" {
			if version == "" || CompareVersions(v, version) > 0 {
				version, source = v, "bundled_manifest"
			}
		}
	}
	return version, source
}

// shouldSkipCodexFixedHTTPVersionLagCooldown protects the account pool when a
// fixed direct HTTP Codex egress request receives a plan-gated-phrase 400 while
// account-synced or bundled metadata already declares a higher Model Minimal
// Client Version than the gateway's Pinned Egress Version.
func shouldSkipCodexFixedHTTPVersionLagCooldown(ctx context.Context, account *Account, requestedModel, modelKey string) bool {
	if !codexFixedHTTPEgressFromContext(ctx) {
		return false
	}
	pinned := codexPinnedEgressVersion()
	minVersion, source := codexModelMinimalClientVersion(account, requestedModel, modelKey)
	if !codexVersionLagsRequirement(pinned, minVersion) {
		return false
	}
	var accountID int64
	if account != nil {
		accountID = account.ID
	}
	slog.Warn("codex_fixed_http_client_version_lag_cooldown_skipped",
		"account_id", accountID,
		"requested_model", strings.TrimSpace(requestedModel),
		"model_key", modelKey,
		"pinned_egress_version", pinned,
		"minimal_client_version", minVersion,
		"source", source,
	)
	return true
}

// shouldSkipCodexClientVersion400Protection reports whether an upstream HTTP 400
// for an OpenAI OAuth-like account is a client-version mismatch (Guard 1) or a
// fixed-HTTP version-lag plan-gated error (Guard 2), meaning the error must pass
// straight through to the caller without model cooldown, temp-unschedulable
// rules, or account failover.
func shouldSkipCodexClientVersion400Protection(ctx context.Context, account *Account, statusCode int, responseBody []byte, requestedModel string) bool {
	if account == nil || statusCode != http.StatusBadRequest || !isOpenAIOAuthAccount(account) {
		return false
	}
	if isOpenAICodexClientVersionMismatchError(statusCode, responseBody) {
		slog.Warn("codex_client_version_mismatch_cooldown_skipped",
			"account_id", account.ID,
			"requested_model", strings.TrimSpace(requestedModel),
			"status_code", statusCode,
			"pinned_egress_version", codexPinnedEgressVersion(),
		)
		return true
	}
	if !isOpenAICodexPlanGatedModelError(statusCode, responseBody) {
		return false
	}
	modelKey := modelRateLimitKeyForUpstreamModelNotFound(ctx, account, requestedModel)
	if modelKey == "" {
		modelKey = strings.TrimSpace(requestedModel)
	}
	return shouldSkipCodexFixedHTTPVersionLagCooldown(ctx, account, requestedModel, modelKey)
}
