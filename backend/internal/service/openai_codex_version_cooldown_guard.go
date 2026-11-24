package service

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
)

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

// isOpenAICodexClientVersionMismatchError reports whether an upstream HTTP 400
// explicitly rejects the request because the Codex client version is outdated
// or below the server's required minimum. Such failures are global to the
// gateway's outbound client version rather than account-specific, so they must
// not trigger 30-minute per-model cooldowns or pool failover.
func isOpenAICodexClientVersionMismatchError(statusCode int, body []byte) bool {
	if statusCode != http.StatusBadRequest {
		return false
	}
	normalized := normalizeModelNotFoundBody(body)
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

// codexModelMinimalClientVersion returns the highest known Model Minimal Client
// Version for the requested or mapped model across account-synced metadata and
// bundled Codex model descriptors, along with its source label.
func codexModelMinimalClientVersion(account *Account, requestedModel, modelKey string) (version, source string) {
	candidates := make([]string, 0, 2)
	if m := strings.TrimSpace(requestedModel); m != "" {
		candidates = append(candidates, m)
	}
	if m := strings.TrimSpace(modelKey); m != "" && (len(candidates) == 0 || candidates[0] != m) {
		candidates = append(candidates, m)
	}
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
	minVersion, source := codexModelMinimalClientVersion(account, requestedModel, modelKey)
	if !codexVersionLagsRequirement(codexExecHTTPVersion, minVersion) {
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
		"pinned_egress_version", codexExecHTTPVersion,
		"minimal_client_version", minVersion,
		"source", source,
	)
	return true
}
