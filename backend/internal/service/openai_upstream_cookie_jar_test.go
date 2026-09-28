package service

import (
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// TestIsAllowedOpenAIUpstreamCookieNameMatchesOfficialAllowlist 锁定官方白名单：
// 只允许 Cloudflare 基础设施 cookie，账号/会话类 cookie 必须被拒绝。
// 对照 codex-rs/http-client/src/chatgpt_cloudflare_cookies.rs 的单测。
func TestIsAllowedOpenAIUpstreamCookieNameMatchesOfficialAllowlist(t *testing.T) {
	for _, name := range []string{
		"__cf_bm", "__cflb", "__cfruid", "__cfseq", "__cfwaitingroom",
		"__oailb", "_cfuvid", "cf_clearance", "cf_ob_info", "cf_use_ob",
		"cf_chl_rc_i",
	} {
		require.True(t, IsAllowedOpenAIUpstreamCookieName(name), name)
	}
	for _, name := range []string{
		"__Secure-next-auth.session-token", "__Host-next-auth.csrf-token",
		"oai-did", "chatgpt_session", "oai-auth-token", "not_cf_clearance", "",
	} {
		require.False(t, IsAllowedOpenAIUpstreamCookieName(name), name)
	}
}

func TestIsChatGPTUpstreamCookieHost(t *testing.T) {
	for _, host := range []string{
		"chatgpt.com", "CHATGPT.com", "foo.chatgpt.com", "chat.openai.com", "chatgpt-staging.com",
	} {
		require.True(t, IsChatGPTUpstreamCookieHost(host), host)
	}
	for _, host := range []string{
		"evilchatgpt.com", "chatgpt.com.evil.test", "api.openai.com", "foo.chat.openai.com", "",
	} {
		require.False(t, IsChatGPTUpstreamCookieHost(host), host)
	}
}

func TestIsValidOpenAIUpstreamCookieValueRejectsHeaderInjection(t *testing.T) {
	require.True(t, IsValidOpenAIUpstreamCookieValue("abc-123_XYZ.4="))
	for _, value := range []string{"", "a b", "a;b", "a,b", `a"b`, `a\b`, "a\r\nb", "a\nb", "中文"} {
		require.False(t, IsValidOpenAIUpstreamCookieValue(value), value)
	}
}

func TestParseOpenAIUpstreamSetCookiesFiltersAndExpires(t *testing.T) {
	now := time.Date(2026, 9, 28, 12, 0, 0, 0, time.UTC)
	headers := http.Header{}
	headers.Add("Set-Cookie", "__cf_bm=bm-value; Path=/; Domain=chatgpt.com; HttpOnly; SameSite=None; Secure; Max-Age=1800")
	headers.Add("Set-Cookie", "__cflb=lb-value; Path=/; Secure")
	headers.Add("Set-Cookie", "_cfuvid=cfuvid-value; Path=/; Domain=chatgpt.com; Secure")
	headers.Add("Set-Cookie", "__Secure-next-auth.session-token=secret; Path=/; Secure; HttpOnly")
	headers.Add("Set-Cookie", "oai-did=did-value; Path=/; Max-Age=31536000")
	headers.Add("Set-Cookie", "cf_clearance=stale; Path=/; Expires=Mon, 28 Sep 2026 11:00:00 GMT")
	headers.Add("Set-Cookie", "__cfseq=gone; Path=/; Max-Age=0")

	cookies := ParseOpenAIUpstreamSetCookies(headers, now)
	require.Len(t, cookies, 3)
	require.Equal(t, "__cf_bm", cookies[0].Name)
	require.Equal(t, "bm-value", cookies[0].Value)
	require.Equal(t, now.Add(30*time.Minute), cookies[0].ExpiresAt)
	require.Equal(t, "__cflb", cookies[1].Name)
	require.True(t, cookies[1].ExpiresAt.IsZero(), "没有 Max-Age/Expires 的 cookie 应为会话级")
	require.Equal(t, "_cfuvid", cookies[2].Name)
}

func TestBuildOpenAIUpstreamCookieHeaderSortedAndSkipsExpired(t *testing.T) {
	now := time.Date(2026, 9, 28, 12, 0, 0, 0, time.UTC)
	cookies := []UpstreamCookie{
		{Name: "_cfuvid", Value: "cfuvid-value"},
		{Name: "__oailb", Value: "oailb-value", ExpiresAt: now.Add(time.Hour)},
		{Name: "__cflb", Value: "lb-value", ExpiresAt: now.Add(time.Hour)},
		{Name: "__cf_bm", Value: "bm-value", ExpiresAt: now.Add(-time.Minute)},
		{Name: "oai-did", Value: "not-whitelisted"},
		{Name: "__cfruid", Value: "bad value"},
	}
	header := BuildOpenAIUpstreamCookieHeader(cookies, now)
	require.Equal(t, "__cflb=lb-value; __oailb=oailb-value; _cfuvid=cfuvid-value", header)
	require.Equal(t, "", BuildOpenAIUpstreamCookieHeader(nil, now))
	require.Equal(t, "", BuildOpenAIUpstreamCookieHeader([]UpstreamCookie{{Name: "__cf_bm", Value: "x", ExpiresAt: now.Add(-time.Second)}}, now))
}

func TestMergeOpenAIUpstreamCookiesIncomingWinsAndCaps(t *testing.T) {
	now := time.Date(2026, 9, 28, 12, 0, 0, 0, time.UTC)
	existing := []UpstreamCookie{
		{Name: "__cf_bm", Value: "old", ExpiresAt: now.Add(10 * time.Minute)},
		{Name: "_cfuvid", Value: "keep"},
	}
	incoming := []UpstreamCookie{
		{Name: "__cf_bm", Value: "new", ExpiresAt: now.Add(30 * time.Minute)},
		{Name: "__cflb", Value: "added", ExpiresAt: now.Add(time.Hour)},
	}
	merged := MergeOpenAIUpstreamCookies(existing, incoming)
	require.Len(t, merged, 3)
	require.Equal(t, "__cf_bm", merged[0].Name)
	require.Equal(t, "new", merged[0].Value)
	require.Equal(t, "__cflb", merged[1].Name)
	require.Equal(t, "_cfuvid", merged[2].Name)

	many := make([]UpstreamCookie, 0, openAIUpstreamCookieJarLimit+5)
	for i := 0; i < openAIUpstreamCookieJarLimit+5; i++ {
		many = append(many, UpstreamCookie{
			Name:      "cf_chl_" + strings.Repeat("a", i+1),
			Value:     "v",
			ExpiresAt: now.Add(time.Duration(i) * time.Minute),
		})
	}
	require.Len(t, MergeOpenAIUpstreamCookies(nil, many), openAIUpstreamCookieJarLimit)
}

func TestOpenAICookieJarNeedsCfuvidSeed(t *testing.T) {
	require.True(t, OpenAICookieJarNeedsCfuvidSeed(nil))
	require.True(t, OpenAICookieJarNeedsCfuvidSeed([]UpstreamCookie{{Name: "__cf_bm", Value: "v"}}))
	require.False(t, OpenAICookieJarNeedsCfuvidSeed([]UpstreamCookie{{Name: "_cfuvid", Value: "v"}}))
}
