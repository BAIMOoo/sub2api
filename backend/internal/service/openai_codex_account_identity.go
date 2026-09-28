package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

const codexAccountIdentityNamespaceVersion = "v1"

const codexAccountIdentitySourceContextKey = "openai_codex_account_identity_source"

// prepareCodexAccountIdentitySource resolves credential shadows once per selected
// attempt. The handler reuses gin.Context across failover attempts, so every entry
// point overwrites the staged source before projecting outbound identity.
func (s *OpenAIGatewayService) prepareCodexAccountIdentitySource(ctx context.Context, c *gin.Context, account *Account) (*Account, error) {
	source := account
	if account != nil && account.IsShadow() {
		resolved, err := resolveCredentialAccount(ctx, s.accountRepo, account)
		if err != nil {
			return nil, err
		}
		source = resolved
	}
	if c != nil {
		c.Set(codexAccountIdentitySourceContextKey, source)
	}
	return source, nil
}

func codexAccountIdentitySource(c *gin.Context, fallback *Account) *Account {
	if c != nil {
		if staged, ok := c.Get(codexAccountIdentitySourceContextKey); ok {
			if source, ok := staged.(*Account); ok && source != nil {
				return source
			}
		}
	}
	return fallback
}

// codexAccountIdentityNamespace returns a stable, credential-scoped namespace.
// Multiple local rows that use the same ChatGPT account intentionally share the
// same namespace. Setup tokens use an irreversible bearer fingerprint because
// they have no refresh lifecycle or imported account metadata. Refreshable OAuth
// otherwise falls back only to a persistent fingerprint seed: local row IDs are
// deployment-relative and must never become upstream identity.
func codexAccountIdentityNamespace(account *Account) string {
	if account == nil || !account.IsOpenAIOAuthLike() {
		return ""
	}
	if upstreamAccountID := strings.TrimSpace(account.GetChatGPTAccountID()); upstreamAccountID != "" {
		if upstreamUserID := strings.TrimSpace(account.GetCredential("chatgpt_user_id")); upstreamUserID != "" {
			return "chatgpt:" + upstreamAccountID + ":user:" + upstreamUserID
		}
		return "chatgpt:" + upstreamAccountID
	}
	if seed, ok := codexFingerprintSeed(account.Extra); ok {
		return "seed:" + seed
	}
	if account.Type == AccountTypeSetupToken {
		if token := strings.TrimSpace(account.GetOpenAIAccessToken()); token != "" {
			sum := sha256.Sum256([]byte("openai-setup-token:" + token))
			return fmt.Sprintf("setup-token:%x", sum[:16])
		}
	}
	return ""
}

// isolateOpenAIUpstreamSessionID preserves the existing API-key isolation while
// adding the selected OAuth credential namespace. A scheduler failover therefore
// cannot send the same session/conversation identity through two upstream accounts.
func isolateOpenAIUpstreamSessionID(apiKeyID int64, account *Account, raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	namespace := codexAccountIdentityNamespace(account)
	if namespace == "" {
		return isolateOpenAISessionID(apiKeyID, raw)
	}
	sum := sha256.Sum256([]byte(fmt.Sprintf("u%d:a%s:%s", apiKeyID, namespace, raw)))
	return fmt.Sprintf("%x", sum[:8])
}

func scopeCodexAccountIdentityValue(account *Account, apiKeyID int64, kind, raw string) string {
	raw = strings.TrimSpace(raw)
	namespace := codexAccountIdentityNamespace(account)
	if raw == "" || namespace == "" {
		return raw
	}
	seed := fmt.Sprintf(
		"sub2api:codex-account-identity:%s:user:%d:account:%s:kind:%s:value:%s",
		codexAccountIdentityNamespaceVersion,
		apiKeyID,
		namespace,
		kind,
		raw,
	)
	// 官方客户端的会话/线程/回合标识都是 UUIDv7（时间戳型）；收敛值保持同一形态，
	// 否则「v4 会话 id」本身就是官方形态里不存在的信号。时间戳沿用原值，
	// 因此同一输入永远得到同一结果，且时间线与客户端原值一致。
	// installation_id 是客户端自带的设备标识（官方为普通 v4），维持派生 v4。
	if kind != codexIdentityKindInstallation {
		if v7, ok := deriveStableCodexIdentityUUIDv7(seed, raw); ok {
			return v7
		}
	}
	return deriveStableUUIDv4(seed)
}

// codexIdentityKindInstallation 是设备/安装标识的作用域 kind：官方为普通 UUIDv4，
// 不参与「保持 v7 形态」的规则。
const codexIdentityKindInstallation = "installation"

// deriveStableCodexIdentityUUIDv7 在原值是 UUIDv7 时派生出同形态的收敛值：
// 沿用原值的 48 位毫秒时间戳，其余比特由种子确定性派生。原值不是 v7 时返回 false，
// 由调用方回退到 UUIDv4 派生（保持既有行为，不凭空造时间戳）。
func deriveStableCodexIdentityUUIDv7(seed, raw string) (string, bool) {
	timestampMillis, ok := uuidV7TimestampMillis(raw)
	if !ok {
		return "", false
	}
	h := sha256.Sum256([]byte(seed))
	b := h[:16]
	b[0] = byte(timestampMillis >> 40)
	b[1] = byte(timestampMillis >> 32)
	b[2] = byte(timestampMillis >> 24)
	b[3] = byte(timestampMillis >> 16)
	b[4] = byte(timestampMillis >> 8)
	b[5] = byte(timestampMillis)
	b[6] = (b[6] & 0x0f) | 0x70 // version 7
	b[8] = (b[8] & 0x3f) | 0x80 // variant 1
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16]), true
}

// uuidV7TimestampMillis 取出 UUIDv7 的 48 位毫秒时间戳；非 v7 形态（或全零时间戳）
// 返回 false。
func uuidV7TimestampMillis(raw string) (uint64, bool) {
	compact := strings.ReplaceAll(strings.TrimSpace(raw), "-", "")
	if len(compact) != 32 || compact[12] != '7' {
		return 0, false
	}
	if _, err := hex.DecodeString(compact); err != nil {
		return 0, false
	}
	timestampMillis, err := strconv.ParseUint(compact[:12], 16, 64)
	if err != nil || timestampMillis == 0 {
		return 0, false
	}
	return timestampMillis, true
}

// scopeCodexAccountIdentityValueKeepSuffix 与 scopeCodexAccountIdentityValue 相同，
// 但保留 "<uuid>:<n>" 形式的后缀序号：官方 x-codex-window-id 与 client_metadata.window_id
// 都是 "<会话 uuid>:<窗口序号>"，整体替换会把 ":0" 抹掉，形成官方不存在的形态。
func scopeCodexAccountIdentityValueKeepSuffix(account *Account, apiKeyID int64, kind, raw string) string {
	trimmed := strings.TrimSpace(raw)
	if base, suffix, ok := strings.Cut(trimmed, ":"); ok && base != "" {
		scoped := scopeCodexAccountIdentityValue(account, apiKeyID, kind, base)
		if scoped != "" {
			return scoped + ":" + suffix
		}
		return raw
	}
	return scopeCodexAccountIdentityValue(account, apiKeyID, kind, raw)
}

// 官方 codex exec 实测（2026-09-28，Linux）的 x-codex-turn-metadata 环境字段取值。
const (
	codexTurnMetadataSandbox     = "seccomp"
	codexTurnMetadataSandboxMode = "workspace-write"
)

// codexTurnMetadataClientOnlyFields 是只有桌面 app 才会带、官方 CLI/exec 从不发送的字段。
// 我们对外声明的是 Linux CLI（exec）身份，留着 app 专有字段会自相矛盾。
var codexTurnMetadataClientOnlyFields = []string{"client_type", "source", "workspace_kind"}

// applyCodexTurnMetadataEnvironment 把出站 x-codex-turn-metadata 的环境字段对齐官方
// exec 实测值（sandbox=seccomp / sandbox_mode=workspace-write），并移除仅桌面 app 才有的
// 字段。客户端透传的 Windows 取值（如 windows_elevated / danger-full-access）与
// 「Linux CLI」身份自相矛盾，是上游可识别的形态差异。
func applyCodexTurnMetadataEnvironment(headers http.Header) bool {
	if headers == nil {
		return false
	}
	raw := strings.TrimSpace(headers.Get("x-codex-turn-metadata"))
	if raw == "" {
		return false
	}
	metadata := map[string]any{}
	if err := json.Unmarshal([]byte(raw), &metadata); err != nil || metadata == nil {
		return false
	}
	changed := false
	if value, ok := metadata["sandbox"].(string); !ok || value != codexTurnMetadataSandbox {
		metadata["sandbox"] = codexTurnMetadataSandbox
		changed = true
	}
	if value, ok := metadata["sandbox_mode"].(string); !ok || value != codexTurnMetadataSandboxMode {
		metadata["sandbox_mode"] = codexTurnMetadataSandboxMode
		changed = true
	}
	for _, field := range codexTurnMetadataClientOnlyFields {
		if _, ok := metadata[field]; ok {
			delete(metadata, field)
			changed = true
		}
	}
	if !changed {
		return false
	}
	rebuilt, err := marshalCodexTurnMetadata(metadata)
	if err != nil {
		return false
	}
	headers.Set("x-codex-turn-metadata", string(rebuilt))
	return true
}

// applyCodexSessionIdentityHeaders 用官方头名重建出站会话身份，并清除 sub2api
// 自造的下划线形态 Session_ID / Conversation_ID（16 位哈希且两者同值，官方客户端
// 在任何模式下都不发送这两个头）。
//
// 官方客户端实测（CLI/TUI 与桌面 app，WS 与 HTTP 两种模式）：session-id /
// thread-id / x-client-request-id 三者恒为同一个 36 字符会话 UUID，
// x-codex-window-id 则是 "<同一 UUID>:<窗口序号>"（首个窗口为 ":0"）。
//
// 这里写入的是客户端原始会话 UUID（缺省时回退 fallbackSessionID，通常是
// prompt_cache_key），作用域化仍由随后的 applyCodexAccountIdentityHeaders 统一完成，
// 因此 API key × OAuth 账号的会话隔离照常生效，同时保留官方"三头同值、窗口带后缀"的关系。
func applyCodexSessionIdentityHeaders(headers http.Header, fallbackSessionID string) {
	if headers == nil {
		return
	}

	// 取值优先级：官方头名 > 客户端下划线形态 > 调用方给的会话回退值（通常是
	// prompt_cache_key，官方客户端里它就是会话 UUID）> 其余同值身份头。
	base := firstNonEmpty(
		headers.Get("session-id"),
		headers.Get("session_id"),
		fallbackSessionID,
		headers.Get("x-client-request-id"),
		headers.Get("thread-id"),
		headers.Get("conversation_id"),
	)
	headers.Del("session_id")
	headers.Del("conversation_id")
	if base == "" {
		return
	}

	// 官方窗口是 "<会话 UUID>:<序号>"：保留客户端已有序号，缺失时按首个窗口 ":0" 补。
	windowSuffix := ":0"
	if _, suffix, ok := strings.Cut(headers.Get("x-codex-window-id"), ":"); ok && suffix != "" {
		windowSuffix = ":" + suffix
	}

	headers.Set("session-id", base)
	headers.Set("thread-id", base)
	headers.Set("x-client-request-id", base)
	headers.Set("x-codex-window-id", base+windowSuffix)
}

var codexAccountIdentityFields = []struct {
	name string
	kind string
}{
	{name: "installation_id", kind: codexIdentityKindInstallation},
	{name: "x-codex-installation-id", kind: codexIdentityKindInstallation},
	// 官方客户端的会话标识是"同值关系"：session-id = thread-id = x-client-request-id，
	// 且 x-codex-window-id / window_id = "<同一 UUID>:<窗口序号>"。共用同一 kind 才能在
	// 作用域化之后仍保持这种相等关系（同 kind + 同原值 → 同派生值）。
	{name: "session_id", kind: "session"},
	{name: "session-id", kind: "session"},
	{name: "thread_id", kind: "session"},
	{name: "thread-id", kind: "session"},
	{name: "window_id", kind: "session"},
	{name: "x-codex-window-id", kind: "session"},
	{name: "x-client-request-id", kind: "session"},
	{name: "turn_id", kind: "turn"},
	{name: "turn-id", kind: "turn"},
	// 官方 turn_id 与 root_turn_id 是同值关系（同一回合 UUID v7，见实测 body 与
	// x-codex-turn-metadata）。纳入同一 kind 才能保证作用域化后仍相等。
	{name: "root_turn_id", kind: "turn"},
	{name: "root-turn-id", kind: "turn"},
}

func applyCodexAccountIdentityFields(values map[string]any, account *Account, apiKeyID int64) bool {
	if values == nil || codexAccountIdentityNamespace(account) == "" {
		return false
	}
	changed := false
	for _, field := range codexAccountIdentityFields {
		raw, ok := values[field.name].(string)
		if !ok || strings.TrimSpace(raw) == "" {
			continue
		}
		next := scopeCodexAccountIdentityValueKeepSuffix(account, apiKeyID, field.kind, raw)
		if next != raw {
			values[field.name] = next
			changed = true
		}
	}
	return changed
}

func applyCodexAccountIdentityEmbeddedMetadata(values map[string]any, account *Account, apiKeyID int64) bool {
	raw, ok := values[openAIWSTurnMetadataHeader].(string)
	if !ok || strings.TrimSpace(raw) == "" {
		return false
	}
	metadata := map[string]any{}
	if err := json.Unmarshal([]byte(raw), &metadata); err != nil || metadata == nil {
		return false
	}
	if !applyCodexAccountIdentityFields(metadata, account, apiKeyID) {
		return false
	}
	rebuilt, err := marshalCodexTurnMetadata(metadata)
	if err != nil {
		return false
	}
	values[openAIWSTurnMetadataHeader] = string(rebuilt)
	return true
}

func applyCodexAccountIdentityClientMetadataMap(requestBody map[string]any, account *Account, apiKeyID int64) bool {
	if requestBody == nil || codexAccountIdentityNamespace(account) == "" {
		return false
	}
	changed := false
	clientMetadata, _ := requestBody["client_metadata"].(map[string]any)
	originalBodySessionID := ""
	if clientMetadata != nil {
		originalBodySessionID, _ = clientMetadata["session_id"].(string)
		if applyCodexAccountIdentityFields(clientMetadata, account, apiKeyID) {
			changed = true
		}
		if applyCodexAccountIdentityEmbeddedMetadata(clientMetadata, account, apiKeyID) {
			changed = true
		}
	}
	if raw, ok := requestBody["prompt_cache_key"].(string); ok && strings.TrimSpace(raw) != "" {
		kind := "prompt-cache"
		if strings.TrimSpace(originalBodySessionID) != "" && raw == originalBodySessionID {
			kind = "session"
		}
		next := scopeCodexAccountIdentityValue(account, apiKeyID, kind, raw)
		if next != raw {
			requestBody["prompt_cache_key"] = next
			changed = true
		}
	}
	return changed
}

// applyCodexAccountIdentityClientMetadataRaw scopes only the small identity
// subobjects with gjson/sjson. The passthrough hot path never unmarshals the
// potentially multi-megabyte request body.
func applyCodexAccountIdentityClientMetadataRaw(body []byte, account *Account, apiKeyID int64) ([]byte, bool, error) {
	if len(body) == 0 || codexAccountIdentityNamespace(account) == "" {
		return body, false, nil
	}
	root := gjson.ParseBytes(body)
	if !root.IsObject() {
		return body, false, nil
	}

	next := body
	changed := false
	originalBodySessionID := ""
	if cm := gjson.GetBytes(body, "client_metadata"); cm.IsObject() {
		clientMetadata := map[string]any{}
		if err := json.Unmarshal([]byte(cm.Raw), &clientMetadata); err != nil {
			return body, false, fmt.Errorf("decode client_metadata for account identity: %w", err)
		}
		originalBodySessionID, _ = clientMetadata["session_id"].(string)
		metadataChanged := applyCodexAccountIdentityFields(clientMetadata, account, apiKeyID)
		if applyCodexAccountIdentityEmbeddedMetadata(clientMetadata, account, apiKeyID) {
			metadataChanged = true
		}
		if metadataChanged {
			raw, err := json.Marshal(clientMetadata)
			if err != nil {
				return body, false, fmt.Errorf("encode account-scoped client_metadata: %w", err)
			}
			var setErr error
			next, setErr = sjson.SetRawBytes(next, "client_metadata", raw)
			if setErr != nil {
				return body, false, fmt.Errorf("splice account-scoped client_metadata: %w", setErr)
			}
			changed = true
		}
	}
	if promptCacheKey := gjson.GetBytes(body, "prompt_cache_key"); promptCacheKey.Type == gjson.String && strings.TrimSpace(promptCacheKey.String()) != "" {
		raw := promptCacheKey.String()
		kind := "prompt-cache"
		if strings.TrimSpace(originalBodySessionID) != "" && raw == originalBodySessionID {
			kind = "session"
		}
		scoped := scopeCodexAccountIdentityValue(account, apiKeyID, kind, raw)
		if scoped != raw {
			rewritten, err := sjson.SetBytes(next, "prompt_cache_key", scoped)
			if err != nil {
				return body, false, fmt.Errorf("splice account-scoped prompt_cache_key: %w", err)
			}
			next = rewritten
			changed = true
		}
	}
	return next, changed, nil
}

func applyCodexAccountIdentityHeaders(headers http.Header, account *Account, apiKeyID int64) {
	if headers == nil || codexAccountIdentityNamespace(account) == "" {
		return
	}
	for _, field := range codexAccountIdentityFields {
		// 下划线会话头由 applyCodexSessionIdentityHeaders 统一清除并用官方头名重建，
		// 不在这里单独作用域化。
		if field.name == "session_id" {
			continue
		}
		raw := strings.TrimSpace(headers.Get(field.name))
		if raw != "" {
			headers.Set(field.name, scopeCodexAccountIdentityValueKeepSuffix(account, apiKeyID, field.kind, raw))
		}
	}
	if raw := strings.TrimSpace(headers.Get(openAIWSTurnMetadataHeader)); raw != "" {
		metadata := map[string]any{}
		if err := json.Unmarshal([]byte(raw), &metadata); err == nil && metadata != nil && applyCodexAccountIdentityFields(metadata, account, apiKeyID) {
			if rebuilt, err := marshalCodexTurnMetadata(metadata); err == nil {
				headers.Set(openAIWSTurnMetadataHeader, string(rebuilt))
			}
		}
	}
}
