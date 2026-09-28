package repository

import (
	"context"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

// fakeUpstreamCookieStore 是内存版 jar，用于验证装饰器的注入/采集/清空行为。
type fakeUpstreamCookieStore struct {
	mu     sync.Mutex
	jar    []service.UpstreamCookie
	saves  int
	clears int
}

func (s *fakeUpstreamCookieStore) LoadUpstreamCookies(context.Context, int64, string) ([]service.UpstreamCookie, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]service.UpstreamCookie(nil), s.jar...), nil
}

func (s *fakeUpstreamCookieStore) SaveUpstreamCookies(_ context.Context, _ int64, _ string, cookies []service.UpstreamCookie) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.jar = append([]service.UpstreamCookie(nil), cookies...)
	s.saves++
	return nil
}

func (s *fakeUpstreamCookieStore) ClearUpstreamCookies(context.Context, int64, string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.jar = nil
	s.clears++
	return nil
}

func (s *fakeUpstreamCookieStore) snapshot() []service.UpstreamCookie {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]service.UpstreamCookie(nil), s.jar...)
}

// recordingUpstream 记录收到的请求，并按脚本返回响应。
type recordingUpstream struct {
	mu       sync.Mutex
	requests []*http.Request
	cookies  []string
	respond  func(req *http.Request) *http.Response
}

func newRecordingUpstream(respond func(req *http.Request) *http.Response) *recordingUpstream {
	return &recordingUpstream{respond: respond}
}

func (u *recordingUpstream) Do(req *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
	u.mu.Lock()
	u.requests = append(u.requests, req)
	u.cookies = append(u.cookies, req.Header.Get("Cookie"))
	u.mu.Unlock()
	return u.respond(req), nil
}

func (u *recordingUpstream) DoWithTLS(req *http.Request, proxyURL string, accountID int64, conc int, _ *tlsfingerprint.Profile) (*http.Response, error) {
	return u.Do(req, proxyURL, accountID, conc)
}

func (u *recordingUpstream) cookieAt(index int) string {
	u.mu.Lock()
	defer u.mu.Unlock()
	if index >= len(u.cookies) {
		return ""
	}
	return u.cookies[index]
}

func (u *recordingUpstream) paths() []string {
	u.mu.Lock()
	defer u.mu.Unlock()
	out := make([]string, 0, len(u.requests))
	for _, req := range u.requests {
		out = append(out, req.URL.Path)
	}
	return out
}

func emptyResponse(status int, header http.Header) *http.Response {
	if header == nil {
		header = http.Header{}
	}
	return &http.Response{StatusCode: status, Header: header, Body: io.NopCloser(strings.NewReader(""))}
}

func newRequest(t *testing.T, method, rawURL string) *http.Request {
	t.Helper()
	req, err := http.NewRequest(method, rawURL, nil)
	require.NoError(t, err)
	req.Header.Set("authorization", "Bearer test-token")
	return req
}

func newJarForTest(inner service.HTTPUpstream, store service.UpstreamCookieStore, now time.Time) *upstreamCookieJar {
	return &upstreamCookieJar{inner: inner, store: store, now: func() time.Time { return now }, lastSeed: map[int64]time.Time{}}
}

const codexResponsesURL = "https://chatgpt.com/backend-api/codex/responses"

func TestUpstreamCookieJarInjectsWhitelistedCookiesOnly(t *testing.T) {
	now := time.Date(2026, 9, 28, 12, 0, 0, 0, time.UTC)
	store := &fakeUpstreamCookieStore{jar: []service.UpstreamCookie{
		{Name: "__cf_bm", Value: "bm", ExpiresAt: now.Add(30 * time.Minute)},
		{Name: "_cfuvid", Value: "cfuvid"},
		{Name: "__cf_bm_expired", Value: "ignored", ExpiresAt: now.Add(-time.Minute)},
	}}
	inner := newRecordingUpstream(func(*http.Request) *http.Response { return emptyResponse(http.StatusOK, nil) })
	jar := newJarForTest(inner, store, now)

	resp, err := jar.Do(newRequest(t, http.MethodPost, codexResponsesURL), "", 9, 1)
	require.NoError(t, err)
	require.NoError(t, resp.Body.Close())
	require.Equal(t, "__cf_bm=bm; _cfuvid=cfuvid", inner.cookieAt(0))
	require.Equal(t, 0, store.saves, "没有 Set-Cookie 时不应写库")
}

func TestUpstreamCookieJarNeverOverridesExplicitCookieHeader(t *testing.T) {
	now := time.Date(2026, 9, 28, 12, 0, 0, 0, time.UTC)
	store := &fakeUpstreamCookieStore{jar: []service.UpstreamCookie{{Name: "__cf_bm", Value: "bm"}}}
	inner := newRecordingUpstream(func(*http.Request) *http.Response { return emptyResponse(http.StatusOK, nil) })
	jar := newJarForTest(inner, store, now)

	req := newRequest(t, http.MethodPost, codexResponsesURL)
	req.Header.Set("Cookie", "explicit=1")
	resp, err := jar.Do(req, "", 9, 1)
	require.NoError(t, err)
	require.NoError(t, resp.Body.Close())
	require.Equal(t, "explicit=1", inner.cookieAt(0))
}

func TestUpstreamCookieJarIgnoresNonChatGPTHosts(t *testing.T) {
	now := time.Date(2026, 9, 28, 12, 0, 0, 0, time.UTC)
	store := &fakeUpstreamCookieStore{jar: []service.UpstreamCookie{{Name: "__cf_bm", Value: "bm"}}}
	inner := newRecordingUpstream(func(*http.Request) *http.Response { return emptyResponse(http.StatusOK, nil) })
	jar := newJarForTest(inner, store, now)

	resp, err := jar.Do(newRequest(t, http.MethodPost, "https://api.openai.com/v1/responses"), "", 9, 1)
	require.NoError(t, err)
	require.NoError(t, resp.Body.Close())
	require.Equal(t, "", inner.cookieAt(0))
}

func TestUpstreamCookieJarHarvestsSetCookie(t *testing.T) {
	now := time.Date(2026, 9, 28, 12, 0, 0, 0, time.UTC)
	store := &fakeUpstreamCookieStore{}
	inner := newRecordingUpstream(func(*http.Request) *http.Response {
		header := http.Header{}
		header.Add("Set-Cookie", "__oailb=routing; Path=/; Max-Age=3600; Secure; HttpOnly; SameSite=Lax")
		header.Add("Set-Cookie", "__cf_bm=bm; Path=/; Domain=chatgpt.com; Max-Age=1800; Secure")
		header.Add("Set-Cookie", "__Secure-next-auth.session-token=secret; Path=/; Secure; HttpOnly")
		return emptyResponse(http.StatusOK, header)
	})
	jar := newJarForTest(inner, store, now)

	resp, err := jar.Do(newRequest(t, http.MethodPost, codexResponsesURL), "", 9, 1)
	require.NoError(t, err)
	require.NoError(t, resp.Body.Close())

	require.Equal(t, 1, store.saves)
	saved := store.snapshot()
	require.Len(t, saved, 2, "会话类 cookie 不得进入 jar")
	require.Equal(t, "__cf_bm", saved[0].Name)
	require.Equal(t, now.Add(30*time.Minute), saved[0].ExpiresAt)
	require.Equal(t, "__oailb", saved[1].Name)
	require.Equal(t, now.Add(time.Hour), saved[1].ExpiresAt)
}

func TestUpstreamCookieJarClearsJarOnChallenge(t *testing.T) {
	now := time.Date(2026, 9, 28, 12, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		name   string
		status int
		header http.Header
	}{
		{name: "403", status: http.StatusForbidden},
		{name: "cf-mitigated", status: http.StatusOK, header: http.Header{"Cf-Mitigated": []string{"challenge"}}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			store := &fakeUpstreamCookieStore{jar: []service.UpstreamCookie{{Name: "__cf_bm", Value: "bm"}}}
			inner := newRecordingUpstream(func(*http.Request) *http.Response { return emptyResponse(tc.status, tc.header) })
			jar := newJarForTest(inner, store, now)

			resp, err := jar.Do(newRequest(t, http.MethodPost, codexResponsesURL), "", 9, 1)
			require.NoError(t, err)
			require.NoError(t, resp.Body.Close())
			require.Equal(t, 1, store.clears)
			require.Empty(t, store.snapshot())
		})
	}
}

func TestUpstreamCookieJarSeedsCfuvidOnceAndOnlyWhenMissing(t *testing.T) {
	now := time.Date(2026, 9, 28, 12, 0, 0, 0, time.UTC)
	store := &fakeUpstreamCookieStore{jar: []service.UpstreamCookie{{Name: "__cf_bm", Value: "bm"}}}
	inner := newRecordingUpstream(func(req *http.Request) *http.Response {
		if req.URL.Path == service.OpenAIUpstreamCookieSeedPath {
			header := http.Header{}
			header.Add("Set-Cookie", "_cfuvid=cfuvid; Path=/; Domain=chatgpt.com; Secure")
			return emptyResponse(http.StatusOK, header)
		}
		return emptyResponse(http.StatusOK, nil)
	})
	jar := newJarForTest(inner, store, now)

	for i := 0; i < 2; i++ {
		resp, err := jar.Do(newRequest(t, http.MethodPost, codexResponsesURL), "", 9, 1)
		require.NoError(t, err)
		require.NoError(t, resp.Body.Close())
	}
	require.Eventually(t, func() bool {
		for _, c := range store.snapshot() {
			if c.Name == "_cfuvid" {
				return true
			}
		}
		return false
	}, 2*time.Second, 10*time.Millisecond, "播种请求应把 _cfuvid 收进 jar")

	seedCalls := 0
	for _, path := range inner.paths() {
		if path == service.OpenAIUpstreamCookieSeedPath {
			seedCalls++
		}
	}
	require.Equal(t, 1, seedCalls, "每个账号每 30 分钟最多播种一次")
}

func TestBuildUpstreamCookieSeedRequestUsesSameIdentityWithoutBody(t *testing.T) {
	req := newRequest(t, http.MethodPost, "https://chatgpt.com/backend-api/codex/responses?trace=1")
	req.Header.Set("chatgpt-account-id", "acct")
	req.Header.Set("user-agent", "codex_exec/0.158.0")
	req.Header.Set("originator", "codex_exec")
	req.Header.Set("content-encoding", "zstd")
	req.Header.Set("cookie", "__cf_bm=bm")

	seed := buildUpstreamCookieSeedRequest(req)
	require.NotNil(t, seed)
	require.Equal(t, http.MethodGet, seed.Method)
	require.Equal(t, service.OpenAIUpstreamCookieSeedPath, seed.URL.Path)
	require.Equal(t, "", seed.URL.RawQuery)
	require.Equal(t, "https://chatgpt.com", seed.URL.Scheme+"://"+seed.URL.Host)
	require.Equal(t, "Bearer test-token", seed.Header.Get("Authorization"))
	require.Equal(t, "acct", seed.Header.Get("Chatgpt-Account-Id"))
	require.Equal(t, "codex_exec", seed.Header.Get("Originator"))
	require.Equal(t, "", seed.Header.Get("Content-Encoding"))
	require.Equal(t, "", seed.Header.Get("Cookie"))
}

func TestNewUpstreamCookieJarWithoutDependenciesReturnsInner(t *testing.T) {
	inner := newRecordingUpstream(func(*http.Request) *http.Response { return emptyResponse(http.StatusOK, nil) })
	require.Same(t, inner, NewUpstreamCookieJar(inner, nil))
	require.Nil(t, NewUpstreamCookieJar(nil, &fakeUpstreamCookieStore{}))
}
