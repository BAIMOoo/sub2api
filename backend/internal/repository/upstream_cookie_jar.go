package repository

import (
	"context"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/Wei-Shaw/sub2api/internal/service"
)

// upstreamCookieSeedInterval 限制同一账号的播种频率（_cfuvid 只在少数路径下发）。
const upstreamCookieSeedInterval = 30 * time.Minute

// upstreamCookieSeedTimeout 是播种请求的超时；播种失败不影响主请求。
const upstreamCookieSeedTimeout = 5 * time.Second

// upstreamCookieJar 是 HTTPUpstream 的装饰器：给发往 chatgpt.com 的请求附带
// Cloudflare 基础设施 cookie，并把上游响应里的 Set-Cookie 收回来。
//
// 设计意图（对齐官方客户端形态，见 internal/service/openai_upstream_cookie_jar.go 的说明）：
//   - 不引入任何登录态语义：jar 里只会有官方白名单内的 Cloudflare cookie；
//   - 采集与注入都在这一处完成，因此天然覆盖 responses / 透传 / 探测等全部出站路径；
//   - 客户端自带的 Cookie 头优先级最高（见 roundTrip 的短路条件），不会被覆盖。
type upstreamCookieJar struct {
	inner service.HTTPUpstream
	store service.UpstreamCookieStore
	now   func() time.Time

	seedMu   sync.Mutex
	lastSeed map[int64]time.Time
}

// NewUpstreamCookieJar 包装一个上游客户端，使其维护 chatgpt.com 的 cookie jar。
func NewUpstreamCookieJar(inner service.HTTPUpstream, store service.UpstreamCookieStore) service.HTTPUpstream {
	if inner == nil || store == nil {
		return inner
	}
	return &upstreamCookieJar{
		inner:    inner,
		store:    store,
		now:      time.Now,
		lastSeed: make(map[int64]time.Time),
	}
}

func (j *upstreamCookieJar) Do(req *http.Request, proxyURL string, accountID int64, accountConcurrency int) (*http.Response, error) {
	return j.roundTrip(req, proxyURL, accountID, nil, func() (*http.Response, error) {
		return j.inner.Do(req, proxyURL, accountID, accountConcurrency)
	})
}

func (j *upstreamCookieJar) DoWithTLS(req *http.Request, proxyURL string, accountID int64, accountConcurrency int, profile *tlsfingerprint.Profile) (*http.Response, error) {
	return j.roundTrip(req, proxyURL, accountID, profile, func() (*http.Response, error) {
		return j.inner.DoWithTLS(req, proxyURL, accountID, accountConcurrency, profile)
	})
}

// profile 是本次主请求使用的 TLS 指纹；播种请求必须沿用同一个形态，
// 否则 chatgpt.com 会周期性看到一个 Go crypto/tls 的握手（与 W1 目标相违背）。
func (j *upstreamCookieJar) roundTrip(req *http.Request, proxyURL string, accountID int64, profile *tlsfingerprint.Profile, call func() (*http.Response, error)) (*http.Response, error) {
	host, target := upstreamCookieJarTarget(req, accountID)
	if target && strings.TrimSpace(req.Header.Get("Cookie")) == "" {
		cookies, err := j.store.LoadUpstreamCookies(req.Context(), accountID, host)
		if err == nil {
			if header := service.BuildOpenAIUpstreamCookieHeader(cookies, j.now()); header != "" {
				req.Header.Set("Cookie", header)
			}
			j.seedMissingCfuvid(req, host, proxyURL, accountID, cookies, profile)
		}
	}

	resp, err := call()
	if err != nil || resp == nil || !target {
		return resp, err
	}
	j.harvestSetCookies(req.Context(), host, accountID, resp)
	return resp, nil
}

// harvestSetCookies 把上游响应的 Set-Cookie 收进 jar；上游明确拒绝时清空 jar 以便自愈。
func (j *upstreamCookieJar) harvestSetCookies(ctx context.Context, host string, accountID int64, resp *http.Response) {
	if resp == nil {
		return
	}
	if resp.StatusCode == http.StatusForbidden || strings.TrimSpace(resp.Header.Get("cf-mitigated")) != "" {
		// 安全网：CF 挑战/403 说明当前 cookie 组合不被接受，直接清空，
		// 下一次请求回到“不带 cookie”的已知可用形态（实测该形态稳定 200）。
		_ = j.store.ClearUpstreamCookies(ctx, accountID, host)
		return
	}
	harvested := service.ParseOpenAIUpstreamSetCookies(resp.Header, j.now())
	if len(harvested) == 0 {
		return
	}
	existing, err := j.store.LoadUpstreamCookies(ctx, accountID, host)
	if err != nil {
		existing = nil
	}
	merged := service.MergeOpenAIUpstreamCookies(existing, harvested)
	_ = j.store.SaveUpstreamCookies(ctx, accountID, host, merged)
}

// seedMissingCfuvid 在 jar 里没有 _cfuvid 时补一次播种请求。
//
// 实测 _cfuvid 只在少数路径下发（响应路径不给），因此这里补一次 GET；端点选的是官方 CLI 实包中
// 出现过、且实测稳定下发 _cfuvid 的插件端点，见 service.OpenAIUpstreamCookieSeedPath 的说明。
// 播种走 goroutine + 独立超时 + 每账号 30 分钟一次，失败只是少一个 cookie，不影响主请求。
func (j *upstreamCookieJar) seedMissingCfuvid(req *http.Request, host, proxyURL string, accountID int64, cookies []service.UpstreamCookie, profile *tlsfingerprint.Profile) {
	if !service.OpenAICookieJarNeedsCfuvidSeed(cookies) {
		return
	}
	if !j.claimSeedSlot(accountID) {
		return
	}
	seedReq := buildUpstreamCookieSeedRequest(req)
	if seedReq == nil {
		return
	}
	// 主请求返回后其 context 会被取消，播种必须脱离它，但仍保留其中的值（trace 等）。
	baseCtx := context.WithoutCancel(req.Context())
	go func() {
		ctx, cancel := context.WithTimeout(baseCtx, upstreamCookieSeedTimeout)
		defer cancel()
		var resp *http.Response
		var err error
		if profile != nil {
			resp, err = j.inner.DoWithTLS(seedReq.WithContext(ctx), proxyURL, accountID, 1, profile)
		} else {
			resp, err = j.inner.Do(seedReq.WithContext(ctx), proxyURL, accountID, 1)
		}
		if err != nil || resp == nil {
			return
		}
		defer func() { _ = resp.Body.Close() }()
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64<<10))
		j.harvestSetCookies(ctx, host, accountID, resp)
	}()
}

func (j *upstreamCookieJar) claimSeedSlot(accountID int64) bool {
	j.seedMu.Lock()
	defer j.seedMu.Unlock()
	if j.lastSeed == nil {
		j.lastSeed = make(map[int64]time.Time)
	}
	now := j.now()
	if last, ok := j.lastSeed[accountID]; ok && now.Sub(last) < upstreamCookieSeedInterval {
		return false
	}
	j.lastSeed[accountID] = now
	return true
}

// upstreamCookieJarTarget 判断请求是否属于 cookie jar 的作用域，返回规范化主机名。
func upstreamCookieJarTarget(req *http.Request, accountID int64) (string, bool) {
	if req == nil || req.URL == nil || accountID <= 0 || req.Method == "" {
		return "", false
	}
	if !strings.EqualFold(req.URL.Scheme, "https") {
		return "", false
	}
	host := strings.ToLower(req.URL.Hostname())
	if !service.IsChatGPTUpstreamCookieHost(host) {
		return "", false
	}
	return host, true
}

// buildUpstreamCookieSeedRequest 由主请求复制出播种请求（同出口、同账号身份，仅换路径与方法）。
func buildUpstreamCookieSeedRequest(req *http.Request) *http.Request {
	if req == nil || req.URL == nil {
		return nil
	}
	seedURL := *req.URL
	seedURL.Path = service.OpenAIUpstreamCookieSeedPath
	seedURL.RawPath = ""
	// 主请求的 query 一律丢弃，只带播种端点自己的官方 query（见 OpenAIUpstreamCookieSeedQuery）。
	seedURL.RawQuery = service.OpenAIUpstreamCookieSeedQuery
	seedURL.Fragment = ""
	seed := &http.Request{
		Method: http.MethodGet,
		URL:    &seedURL,
		Header: make(http.Header, 8),
		Host:   req.Host,
	}
	// 只复制身份类头；正文相关的头（content-type/content-encoding 等）一律不带。
	for _, name := range []string{"Authorization", "Chatgpt-Account-Id", "User-Agent", "Originator", "Version", "Accept-Language"} {
		if value := req.Header.Get(name); value != "" {
			seed.Header.Set(name, value)
		}
	}
	seed.Header.Set("Accept", "*/*")
	return seed
}
