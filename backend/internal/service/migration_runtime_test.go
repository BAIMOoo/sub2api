package service

import (
	"bufio"
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/stretchr/testify/require"
)

type migrationConcurrencyCache struct {
	ConcurrencyCache
	cleanupPrefix string
}

func (c *migrationConcurrencyCache) CleanupStaleProcessSlots(_ context.Context, prefix string) error {
	c.cleanupPrefix = prefix
	return nil
}

func TestMigrationStartupCleanupPreservesOtherProcessesOnlyWhenEnabled(t *testing.T) {
	t.Setenv("MIGRATION_RUNTIME_ADDR", "")
	for _, tc := range []struct {
		name, value string
		wantCleanup bool
	}{
		{name: "legacy default", wantCleanup: true},
		{name: "explicit legacy", value: "false", wantCleanup: true},
		{name: "shared redis", value: "true"},
		{name: "invalid fails conservatively", value: "tru"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("MIGRATION_SHARED_REDIS", tc.value)
			cache := &migrationConcurrencyCache{}
			ProvideConcurrencyService(cache, nil, nil)
			require.Equal(t, tc.wantCleanup, cache.cleanupPrefix != "")
		})
	}
}

func TestMigrationControlModeAlsoPreservesOtherProcessesSlots(t *testing.T) {
	t.Setenv("MIGRATION_SHARED_REDIS", "false")
	t.Setenv("MIGRATION_RUNTIME_ADDR", "127.0.0.1:9009")
	cache := &migrationConcurrencyCache{}
	ProvideConcurrencyService(cache, nil, nil)
	require.Empty(t, cache.cleanupPrefix)
}

func TestMigrationConfigRequiresPrivateControlAndValidSwitch(t *testing.T) {
	t.Setenv("MIGRATION_BACKGROUND_WORKERS", "")
	for _, tc := range []struct {
		name, addr, token, shared string
		wantError                 bool
	}{
		{name: "disabled"},
		{name: "loopback IPv4", addr: "127.0.0.1:9009", token: strings.Repeat("a", 32)},
		{name: "loopback IPv6", addr: "[::1]:9009", token: strings.Repeat("a", 32)},
		{name: "public control", addr: "0.0.0.0:9009", token: strings.Repeat("a", 32), wantError: true},
		{name: "ambiguous host", addr: "localhost:9009", token: strings.Repeat("a", 32), wantError: true},
		{name: "missing token", addr: "127.0.0.1:9009", wantError: true},
		{name: "weak token", addr: "127.0.0.1:9009", token: "short", wantError: true},
		{name: "invalid shared mode", shared: "tru", wantError: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("MIGRATION_RUNTIME_ADDR", tc.addr)
			t.Setenv("MIGRATION_RUNTIME_TOKEN", tc.token)
			t.Setenv("MIGRATION_SHARED_REDIS", tc.shared)
			_, err := MigrationRuntimeConfigFromEnv()
			if tc.wantError {
				require.Error(t, err)
			} else {
				require.NoError(t, err)
			}
		})
	}
}

func TestMigrationControlStartCannotBypassPrivateBindValidation(t *testing.T) {
	_, err := NewMigrationRuntime().StartControl(MigrationRuntimeConfig{
		ListenAddress: "0.0.0.0:9009", Token: strings.Repeat("a", 32),
	})
	require.ErrorContains(t, err, "loopback")
}

func TestMigrationDrainObservesRealStreamingRequestsWithoutInterrupting(t *testing.T) {
	runtime := NewMigrationRuntime()
	started := make(chan struct{})
	release := make(chan struct{})
	finished := make(chan struct{})
	handler := runtime.WrapHandler(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		_, _ = io.WriteString(w, "data: before\n\n")
		w.(http.Flusher).Flush()
		close(started)
		<-release
		_, _ = io.WriteString(w, "data: after\n\n")
	}))
	w := httptest.NewRecorder()
	go func() {
		defer close(finished)
		handler.ServeHTTP(w, httptest.NewRequest(http.MethodGet, "/v1/responses", nil))
	}()
	<-started
	require.EqualValues(t, 1, runtime.Snapshot().ActiveRequests)
	runtime.BeginDrain()
	require.True(t, runtime.Snapshot().Draining)
	require.False(t, runtime.Snapshot().SafeToStop)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	require.ErrorIs(t, runtime.WaitForRequests(ctx), context.Canceled)
	close(release)
	<-finished
	require.NoError(t, runtime.WaitForRequests(context.Background()))
	require.Contains(t, w.Body.String(), "data: before\n\ndata: after\n\n")
	require.Zero(t, runtime.Snapshot().ActiveRequests)
}

func TestMigrationControlAuthenticationAndReadinessDoNotRejectBusiness(t *testing.T) {
	setMigrationReadyProbesForTest(t)
	runtime := NewMigrationRuntime()
	runtime.MarkServing()
	control := runtime.ControlHandler("private-token")
	request := func(method, path, token string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(method, path, nil)
		if token != "" {
			r.Header.Set("Authorization", "Bearer "+token)
		}
		w := httptest.NewRecorder()
		control.ServeHTTP(w, r)
		return w
	}
	require.Equal(t, http.StatusUnauthorized, request(http.MethodPost, "/drain", "").Code)
	require.False(t, runtime.Snapshot().Draining)
	require.Equal(t, http.StatusMethodNotAllowed, request(http.MethodGet, "/drain", "private-token").Code)
	require.Equal(t, http.StatusOK, request(http.MethodGet, "/ready", "private-token").Code)
	require.Equal(t, http.StatusOK, request(http.MethodPost, "/drain", "private-token").Code)
	require.Equal(t, http.StatusServiceUnavailable, request(http.MethodGet, "/ready", "private-token").Code)
	business := httptest.NewRecorder()
	runtime.WrapHandler(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	})).ServeHTTP(business, httptest.NewRequest(http.MethodGet, "/v1/models", nil))
	require.Equal(t, http.StatusOK, business.Code)
	require.Equal(t, http.StatusOK, request(http.MethodPost, "/resume", "private-token").Code)
	require.False(t, runtime.Snapshot().Draining)
	require.Contains(t, request(http.MethodGet, "/status", "private-token").Body.String(), `"background_task_coverage":"unavailable"`)
}

func setMigrationReadyProbesForTest(t *testing.T) {
	t.Helper()
	migrationControlRegistry.Lock()
	previous := migrationControlRegistry.readiness
	migrationControlRegistry.readiness = make(map[string]func() error)
	migrationControlRegistry.Unlock()
	t.Cleanup(func() {
		migrationControlRegistry.Lock()
		migrationControlRegistry.readiness = previous
		migrationControlRegistry.Unlock()
	})
	for _, name := range []string{"postgres", "redis", "usage_journal"} {
		RegisterMigrationReadinessProbe(name, func() error { return nil })
	}
}

func TestMigrationReadinessRequiresAllCriticalProbes(t *testing.T) {
	setMigrationReadyProbesForTest(t)
	runtime := NewMigrationRuntime()
	require.False(t, runtime.Snapshot().Ready, "the business listener has not been bound")
	runtime.MarkServing()
	require.True(t, runtime.Snapshot().Ready)
	migrationControlRegistry.Lock()
	delete(migrationControlRegistry.readiness, "usage_journal")
	migrationControlRegistry.Unlock()
	require.False(t, runtime.Snapshot().Ready)
	RegisterMigrationReadinessProbe("usage_journal", func() error { return io.ErrUnexpectedEOF })
	require.False(t, runtime.Snapshot().Ready)
}

func TestMigrationComponentControlIsAlsoAuthenticated(t *testing.T) {
	const prefix = "/data/runtime-test/"
	RegisterMigrationControlHandler(prefix, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusAccepted)
	}))
	t.Cleanup(func() {
		migrationControlRegistry.Lock()
		delete(migrationControlRegistry.handlers, prefix)
		migrationControlRegistry.Unlock()
	})
	control := NewMigrationRuntime().ControlHandler("secret")
	r := httptest.NewRequest(http.MethodPost, prefix+"switch", nil)
	w := httptest.NewRecorder()
	control.ServeHTTP(w, r)
	require.Equal(t, http.StatusUnauthorized, w.Code)
	r.Header.Set("Authorization", "Bearer secret")
	w = httptest.NewRecorder()
	control.ServeHTTP(w, r)
	require.Equal(t, http.StatusAccepted, w.Code)
}

type migrationHijackWriter struct {
	*httptest.ResponseRecorder
	conn net.Conn
}

func (w *migrationHijackWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	return w.conn, bufio.NewReadWriter(bufio.NewReader(w.conn), bufio.NewWriter(w.conn)), nil
}

func TestMigrationHijackedConnectionsBlockRequestDrainUntilClosed(t *testing.T) {
	runtime := NewMigrationRuntime()
	serverConn, clientConn := net.Pipe()
	t.Cleanup(func() { _ = clientConn.Close() })
	var hijacked net.Conn
	handler := runtime.WrapHandler(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		var err error
		hijacked, _, err = w.(http.Hijacker).Hijack()
		require.NoError(t, err)
	}))
	handler.ServeHTTP(&migrationHijackWriter{httptest.NewRecorder(), serverConn}, httptest.NewRequest(http.MethodGet, "/ws", nil))
	require.Zero(t, runtime.Snapshot().ActiveRequests)
	require.EqualValues(t, 1, runtime.Snapshot().ActiveHijackedConnections)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	require.ErrorIs(t, runtime.WaitForRequests(ctx), context.DeadlineExceeded)
	require.NoError(t, hijacked.Close())
	_ = hijacked.Close()
	require.NoError(t, runtime.WaitForRequests(context.Background()))
	require.Zero(t, runtime.Snapshot().ActiveHijackedConnections)
}

func TestMigrationTaskProbeNeverClaimsCompleteWriteDrain(t *testing.T) {
	runtime := NewMigrationRuntime()
	runtime.RegisterTaskProbe("usage", func() MigrationTaskStats {
		return MigrationTaskStats{Active: 2, Pending: 3}
	})
	status := runtime.Snapshot()
	require.Equal(t, "partial", status.BackgroundTaskCoverage)
	require.EqualValues(t, 2, status.Tasks["usage"].Active)
	require.EqualValues(t, 3, status.Tasks["usage"].Pending)
	require.False(t, status.SafeToStop)
}

func TestMigrationBackgroundWorkersAreOptInForReplicas(t *testing.T) {
	for _, tc := range []struct {
		name, shared, control, workers string
		want                           bool
	}{
		{name: "legacy default", want: true},
		{name: "shared redis replica", shared: "true"},
		{name: "control runtime replica", control: "127.0.0.1:9009"},
		{name: "explicit worker owner", shared: "true", workers: "true", want: true},
		{name: "explicit workers disabled", workers: "false"},
		{name: "invalid disables conservatively", workers: "tru"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("MIGRATION_SHARED_REDIS", tc.shared)
			t.Setenv("MIGRATION_RUNTIME_ADDR", tc.control)
			t.Setenv("MIGRATION_BACKGROUND_WORKERS", tc.workers)
			calls := 0
			startMigrationBackgroundWorker("test", func() { calls++ })
			require.Equal(t, tc.want, calls == 1)
		})
	}
}

type migrationExpiryAccountRepo struct {
	AccountRepository
	calls atomic.Int64
}

func (r *migrationExpiryAccountRepo) AutoPauseExpiredAccounts(context.Context, time.Time) (int64, error) {
	r.calls.Add(1)
	return 0, nil
}

type migrationExpiryProxyRepo struct {
	ProxyRepository
	calls atomic.Int64
}

func (r *migrationExpiryProxyRepo) SweepExpiredProxies(context.Context, time.Time) (int64, error) {
	r.calls.Add(1)
	return 0, nil
}

func TestMigrationReplicaDoesNotStartExpiryWriters(t *testing.T) {
	t.Setenv("MIGRATION_SHARED_REDIS", "true")
	t.Setenv("MIGRATION_BACKGROUND_WORKERS", "")
	accountRepo := &migrationExpiryAccountRepo{}
	proxyRepo := &migrationExpiryProxyRepo{}
	accounts := ProvideAccountExpiryService(accountRepo)
	proxies := ProvideProxyExpiryService(proxyRepo)
	accounts.Stop()
	proxies.Stop()
	require.Zero(t, accountRepo.calls.Load())
	require.Zero(t, proxyRepo.calls.Load())
}

func TestMigrationDisabledQuotaFlusherDoesNotTouchDirtyKeysOnStop(t *testing.T) {
	t.Setenv("MIGRATION_SHARED_REDIS", "true")
	t.Setenv("MIGRATION_BACKGROUND_WORKERS", "")
	// Nil dependencies make accidental timer registration or final flushing
	// fail immediately, rather than silently consuming shared dirty keys.
	cfg := &config.Config{}
	cfg.Database.UserPlatformQuotaFlusherEnabled = true
	flusher := ProvideUserPlatformQuotaUsageFlusher(cfg, nil, nil, nil)
	require.True(t, flusher.migrationDisabled.Load())
	require.NotPanics(t, flusher.Stop)
}

func TestMigrationPricingUsesCopiedCatalogueWithoutRemoteUpdate(t *testing.T) {
	t.Setenv("MIGRATION_SHARED_REDIS", "true")
	t.Setenv("MIGRATION_BACKGROUND_WORKERS", "")
	dir := t.TempDir()
	path := filepath.Join(dir, "model_pricing.json")
	body := []byte(`{"migration-model":{"input_cost_per_token":0.125,"output_cost_per_token":0.5,"litellm_provider":"openai","mode":"chat"}}`)
	require.NoError(t, os.WriteFile(path, body, 0600))
	cfg := &config.Config{Pricing: config.PricingConfig{
		DataDir: dir, RemoteURL: "https://must-not-be-called.invalid/prices", HashURL: "https://must-not-be-called.invalid/hash",
	}}
	// A nil remote client would panic if startup made an upstream request.
	svc, err := ProvidePricingService(cfg, nil)
	require.NoError(t, err)
	t.Cleanup(svc.Stop)
	require.InDelta(t, 0.125, svc.GetModelPricing("migration-model").InputCostPerToken, 1e-10)
	after, err := os.ReadFile(path)
	require.NoError(t, err)
	require.Equal(t, body, after)
	cfg.Pricing.DataDir = t.TempDir()
	_, err = ProvidePricingService(cfg, nil)
	require.ErrorContains(t, err, "migration requires the existing pricing catalogue")
}

type migrationAuditRepo struct {
	AuditLogRepository
	writes atomic.Int64
}

func (r *migrationAuditRepo) BatchInsert(_ context.Context, logs []*AuditLog) (int64, error) {
	r.writes.Add(int64(len(logs)))
	return int64(len(logs)), nil
}

func TestMigrationReplicaStillPersistsRequestAuditWrites(t *testing.T) {
	t.Setenv("MIGRATION_SHARED_REDIS", "true")
	t.Setenv("MIGRATION_BACKGROUND_WORKERS", "")
	repo := &migrationAuditRepo{}
	svc := ProvideAuditLogService(repo, nil)
	svc.Record(&AuditLog{})
	svc.Stop()
	require.EqualValues(t, 1, repo.writes.Load())
}
