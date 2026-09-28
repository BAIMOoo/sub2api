package service

import (
	"context"
	"net/http"
	"sort"
	"strings"
	"time"
)

// 本文件是「发往 chatgpt.com 的出站 cookie jar」的纯逻辑部分：
// 名字白名单、Set-Cookie 解析、Cookie 头拼装、集合合并。
//
// 对齐依据（官方 codex 客户端源码）：
//
//	codex-rs/http-client/src/chatgpt_cloudflare_cookies.rs
//
// 官方把 ChatGPT 出站 cookie 限制成一个“只允许 Cloudflare 基础设施 cookie +
// __oailb 路由 cookie”的白名单 jar，并显式拒绝账号/会话类 cookie
// （其单测用 __Secure-next-auth.session-token 做反例）。因此这里不引入任何
// 登录态语义，只是复刻官方机制：自己请求拿到什么就回放什么。
//
// 安全约定：cookie 值只允许存在于内存与密文存储中；任何日志、错误信息、
// 指标、调试输出都不得包含 cookie 值。本文件所有函数都是纯函数，不产生输出。

// OpenAIUpstreamCookieHost 是对齐目标主机（官方 jar 的作用域之一）。
const OpenAIUpstreamCookieHost = "chatgpt.com"

// OpenAIUpstreamCookieSeedPath 是补齐 _cfuvid 的“播种”路径。
//
// 实测：Codex responses 路径下发的 Set-Cookie 只有 __oailb/__cf_bm/__cflb；
// _cfuvid 只在 GET / 与 GET /backend-api/me 这类路径下发。官方客户端同样会访问
// 这类接口，因此这里选用带鉴权的 /backend-api/me 作为播种路径。
const OpenAIUpstreamCookieSeedPath = "/backend-api/me"

// openAIUpstreamCookieJarLimit 限制单个 jar 保存的名字数量，避免异常上游把表撑爆。
const openAIUpstreamCookieJarLimit = 16

// UpstreamCookie 是单个出站 cookie。
//
// ExpiresAt 为零值表示会话级 cookie（没有 Max-Age / Expires）。
type UpstreamCookie struct {
	Name      string
	Value     string
	ExpiresAt time.Time
}

// UpstreamCookieStore 按 (账号, 主机) 读写 cookie jar。
//
// 实现层负责加密落盘；本接口只暴露明文，且绝不把值写进日志。
type UpstreamCookieStore interface {
	LoadUpstreamCookies(ctx context.Context, accountID int64, host string) ([]UpstreamCookie, error)
	SaveUpstreamCookies(ctx context.Context, accountID int64, host string, cookies []UpstreamCookie) error
	ClearUpstreamCookies(ctx context.Context, accountID int64, host string) error
}

// IsChatGPTUpstreamCookieHost 判断主机是否属于官方 jar 的作用域。
//
// 与 codex-rs/http-client/src/chatgpt_hosts.rs 的 is_allowed_chatgpt_host 对齐。
func IsChatGPTUpstreamCookieHost(host string) bool {
	host = strings.ToLower(strings.TrimSpace(host))
	if host == "" {
		return false
	}
	switch host {
	case "chatgpt.com", "chat.openai.com", "chatgpt-staging.com":
		return true
	}
	for _, suffix := range []string{".chatgpt.com", ".chatgpt-staging.com"} {
		if strings.HasSuffix(host, suffix) {
			return true
		}
	}
	return false
}

// IsAllowedOpenAIUpstreamCookieName 复刻官方的 Cloudflare cookie 白名单。
//
// 来源：codex-rs/http-client/src/chatgpt_cloudflare_cookies.rs:is_allowed_cloudflare_cookie_name
// （官方另有“配置注入”的 oai-chat-psp，受 Feature::Psp 开关控制，本项目不引入）。
func IsAllowedOpenAIUpstreamCookieName(name string) bool {
	switch name {
	case "__cf_bm",
		"__cflb",
		"__cfruid",
		"__cfseq",
		"__cfwaitingroom",
		"__oailb",
		"_cfuvid",
		"cf_clearance",
		"cf_ob_info",
		"cf_use_ob":
		return true
	}
	return strings.HasPrefix(name, "cf_chl_")
}

// IsValidOpenAIUpstreamCookieValue 校验 cookie 值是否可以安全地放进请求头。
//
// 值来自上游响应或密文存储；这里做一次“可打印可见 ASCII、且不含分隔符”的白名单校验，
// 避免把 CR/LF 之类的字节注回出站请求头（header injection）。
func IsValidOpenAIUpstreamCookieValue(value string) bool {
	if value == "" {
		return false
	}
	for i := 0; i < len(value); i++ {
		b := value[i]
		if b < 0x21 || b > 0x7e {
			return false
		}
		switch b {
		case ';', ',', '"', '\\':
			return false
		}
	}
	return true
}

// ParseOpenAIUpstreamSetCookies 从上游响应头里挑出白名单内、且未过期的 cookie。
//
// 过期判定：Max-Age < 0 视为删除指令（丢弃）；Max-Age > 0 优先于 Expires。
func ParseOpenAIUpstreamSetCookies(headers http.Header, now time.Time) []UpstreamCookie {
	if headers == nil {
		return nil
	}
	resp := http.Response{Header: headers}
	var out []UpstreamCookie
	for _, c := range resp.Cookies() {
		if c == nil {
			continue
		}
		if !IsAllowedOpenAIUpstreamCookieName(c.Name) {
			continue
		}
		if !IsValidOpenAIUpstreamCookieValue(c.Value) {
			continue
		}
		var expiresAt time.Time
		switch {
		case c.MaxAge < 0:
			continue
		case c.MaxAge > 0:
			expiresAt = now.Add(time.Duration(c.MaxAge) * time.Second)
		case !c.Expires.IsZero():
			expiresAt = c.Expires.UTC()
		}
		if !expiresAt.IsZero() && !expiresAt.After(now) {
			continue
		}
		out = append(out, UpstreamCookie{Name: c.Name, Value: c.Value, ExpiresAt: expiresAt})
	}
	return dedupeOpenAIUpstreamCookies(out)
}

// BuildOpenAIUpstreamCookieHeader 把 jar 拼成 Cookie 头（名字排序、跳过过期项）。
//
// 返回空串表示不应附带 Cookie 头。
func BuildOpenAIUpstreamCookieHeader(cookies []UpstreamCookie, now time.Time) string {
	usable := make([]UpstreamCookie, 0, len(cookies))
	for _, c := range cookies {
		if !IsAllowedOpenAIUpstreamCookieName(c.Name) || !IsValidOpenAIUpstreamCookieValue(c.Value) {
			continue
		}
		if !c.ExpiresAt.IsZero() && !c.ExpiresAt.After(now) {
			continue
		}
		usable = append(usable, c)
	}
	usable = dedupeOpenAIUpstreamCookies(usable)
	if len(usable) == 0 {
		return ""
	}
	// 名字排序保证出站 Cookie 头稳定（同一 jar 每次拼出完全相同的字节序）。
	sort.SliceStable(usable, func(i, j int) bool { return usable[i].Name < usable[j].Name })
	pairs := make([]string, 0, len(usable))
	for _, c := range usable {
		pairs = append(pairs, c.Name+"="+c.Value)
	}
	return strings.Join(pairs, "; ")
}

// MergeOpenAIUpstreamCookies 用新收到的 cookie 覆盖同名旧值，并返回排序后的新集合。
//
// 规则：incoming 优先；结果按名字排序；超过上限时保留“有到期时间更晚者优先”的前 N 个。
func MergeOpenAIUpstreamCookies(existing, incoming []UpstreamCookie) []UpstreamCookie {
	merged := make(map[string]UpstreamCookie, len(existing)+len(incoming))
	for _, c := range existing {
		merged[c.Name] = c
	}
	for _, c := range incoming {
		merged[c.Name] = c
	}
	out := make([]UpstreamCookie, 0, len(merged))
	for _, c := range merged {
		out = append(out, c)
	}
	out = dedupeOpenAIUpstreamCookies(out)
	if len(out) > openAIUpstreamCookieJarLimit {
		sort.SliceStable(out, func(i, j int) bool {
			if out[i].ExpiresAt.Equal(out[j].ExpiresAt) {
				return out[i].Name < out[j].Name
			}
			return out[i].ExpiresAt.After(out[j].ExpiresAt)
		})
		out = out[:openAIUpstreamCookieJarLimit]
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// OpenAICookieJarNeedsCfuvidSeed 判断是否还需要一次播种请求（jar 里还没有 _cfuvid）。
func OpenAICookieJarNeedsCfuvidSeed(cookies []UpstreamCookie) bool {
	for _, c := range cookies {
		if c.Name == "_cfuvid" {
			return false
		}
	}
	return true
}

func dedupeOpenAIUpstreamCookies(cookies []UpstreamCookie) []UpstreamCookie {
	if len(cookies) <= 1 {
		return cookies
	}
	seen := make(map[string]int, len(cookies))
	out := make([]UpstreamCookie, 0, len(cookies))
	for _, c := range cookies {
		if idx, ok := seen[c.Name]; ok {
			out[idx] = c
			continue
		}
		seen[c.Name] = len(out)
		out = append(out, c)
	}
	return out
}
