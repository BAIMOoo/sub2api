package service

import (
	"bufio"
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/felixge/httpsnoop"
)

// MigrationModeEnabled intentionally does not affect the existing deployment
// until a migration-specific switch or control listener has been configured.
func MigrationModeEnabled() bool {
	return MigrationSharedRedisEnabled() || strings.TrimSpace(os.Getenv("MIGRATION_RUNTIME_ADDR")) != ""
}

func MigrationBackgroundWorkersEnabled() bool {
	migrationStartedBackgroundWorkers.Lock()
	activated := migrationStartedBackgroundWorkers.activated
	migrationStartedBackgroundWorkers.Unlock()
	if activated {
		return true
	}
	return migrationBackgroundWorkersConfigured()
}

func migrationBackgroundWorkersConfigured() bool {
	if raw := strings.TrimSpace(os.Getenv("MIGRATION_BACKGROUND_WORKERS")); raw != "" {
		enabled, err := strconv.ParseBool(raw)
		return err == nil && enabled
	}
	return !MigrationModeEnabled()
}

func startMigrationBackgroundWorker(name string, start func()) {
	migrationStartedBackgroundWorkers.Lock()
	if migrationStartedBackgroundWorkers.activated || migrationBackgroundWorkersConfigured() {
		migrationStartedBackgroundWorkers.names[name] = true
		migrationStartedBackgroundWorkers.Unlock()
		start()
		return
	}
	migrationStartedBackgroundWorkers.deferred[name] = start
	migrationStartedBackgroundWorkers.Unlock()
	logger.LegacyPrintf("service.migration", "Migration mode: background worker %s is disabled", name)
}

var migrationStartedBackgroundWorkers = struct {
	sync.Mutex
	names      map[string]bool
	deferred   map[string]func()
	failures   map[string]string
	activated  bool
	activating bool
}{names: make(map[string]bool), deferred: make(map[string]func()), failures: make(map[string]string)}

// A quiet handover requires scheduled writers to have been disabled before
// construction. Changing an environment variable after starting them cannot
// manufacture a producer fence.
func MigrationScheduledWritersQuiet() error {
	if MigrationBackgroundWorkersEnabled() {
		return fmt.Errorf("migration scheduled writers are enabled")
	}
	migrationStartedBackgroundWorkers.Lock()
	defer migrationStartedBackgroundWorkers.Unlock()
	if len(migrationStartedBackgroundWorkers.names) != 0 {
		return fmt.Errorf("scheduled writers were started in this process; restart with migration background workers disabled")
	}
	return nil
}

// MigrationSharedRedisEnabled disables destructive startup cleanup while two
// processes share Redis. Invalid nonempty values are conservative here; main
// validates and rejects them before constructing any services.
func MigrationSharedRedisEnabled() bool {
	raw := strings.TrimSpace(os.Getenv("MIGRATION_SHARED_REDIS"))
	if raw == "" {
		return false
	}
	enabled, err := strconv.ParseBool(raw)
	return err != nil || enabled
}

type MigrationRuntimeConfig struct {
	ListenAddress string
	Token         string
}

// MigrationRuntimeConfigFromEnv keeps all migration controls opt-in and refuses
// public binds. Tokens must be independently generated, not existing API keys.
func MigrationRuntimeConfigFromEnv() (MigrationRuntimeConfig, error) {
	var cfg MigrationRuntimeConfig
	if mode := strings.TrimSpace(os.Getenv("MIGRATION_HANDOVER_MODE")); mode != "" && mode != "quiet" && mode != "journal" {
		return cfg, fmt.Errorf("MIGRATION_HANDOVER_MODE must be quiet or journal")
	}
	for _, name := range []string{"MIGRATION_SHARED_REDIS", "MIGRATION_BACKGROUND_WORKERS"} {
		if raw := strings.TrimSpace(os.Getenv(name)); raw != "" {
			if _, err := strconv.ParseBool(raw); err != nil {
				return cfg, fmt.Errorf("%s must be a boolean", name)
			}
		}
	}
	cfg.ListenAddress = strings.TrimSpace(os.Getenv("MIGRATION_RUNTIME_ADDR"))
	cfg.Token = os.Getenv("MIGRATION_RUNTIME_TOKEN")
	if cfg.ListenAddress == "" {
		if MigrationQuietHandoverEnabled() {
			return cfg, fmt.Errorf("quiet migration requires a private control listener")
		}
		return cfg, nil
	}
	return cfg, validateMigrationControlConfig(cfg)
}

func validateMigrationControlConfig(cfg MigrationRuntimeConfig) error {
	host, port, err := net.SplitHostPort(cfg.ListenAddress)
	if err != nil {
		return fmt.Errorf("MIGRATION_RUNTIME_ADDR requires a loopback IP and port")
	}
	ip := net.ParseIP(host)
	if ip == nil || !ip.IsLoopback() {
		return fmt.Errorf("MIGRATION_RUNTIME_ADDR must bind an explicit loopback IP")
	}
	portNumber, err := strconv.Atoi(port)
	if err != nil || portNumber < 1 || portNumber > 65535 {
		return fmt.Errorf("MIGRATION_RUNTIME_ADDR requires a valid port")
	}
	if len(cfg.Token) < 32 {
		return fmt.Errorf("MIGRATION_RUNTIME_TOKEN requires at least 32 bytes")
	}
	return nil
}

type MigrationTaskStats struct {
	Deferred uint64 `json:"deferred,omitempty"`
	Active   uint64 `json:"active"`
	Pending  uint64 `json:"pending"`
	Failed   uint64 `json:"failed"`
	Dropped  uint64 `json:"dropped"`
}

type MigrationRuntimeSnapshot struct {
	InstanceID                string                        `json:"instance_id"`
	Draining                  bool                          `json:"draining"`
	Serving                   bool                          `json:"serving"`
	ActiveRequests            int64                         `json:"active_requests"`
	ActiveHijackedConnections int64                         `json:"active_hijacked_connections"`
	BackgroundTaskCoverage    string                        `json:"background_task_coverage"`
	Tasks                     map[string]MigrationTaskStats `json:"tasks,omitempty"`
	SafeToStop                bool                          `json:"safe_to_stop"`
	BackgroundWorkersEnabled  bool                          `json:"background_workers_enabled"`
	Ready                     bool                          `json:"ready"`
	Readiness                 map[string]bool               `json:"readiness"`
	Ingress                   MigrationIngressSnapshot      `json:"ingress"`
}

// MigrationRuntime observes this process only. Even zero request counts cannot
// prove all scheduled jobs, async writes, or remote writers have stopped.
// SafeToStop therefore deliberately remains false.
type MigrationRuntime struct {
	mu                 sync.Mutex
	draining           bool
	serving            bool
	requests           int64
	hijacked           int64
	changed            chan struct{}
	taskProbes         map[string]func() MigrationTaskStats
	ingress            migrationIngressState
	ingressResumeGuard func() error
}

func NewMigrationRuntime() *MigrationRuntime {
	return &MigrationRuntime{
		changed:    make(chan struct{}),
		taskProbes: make(map[string]func() MigrationTaskStats),
	}
}

func (m *MigrationRuntime) notifyLocked() {
	close(m.changed)
	m.changed = make(chan struct{})
}

func (m *MigrationRuntime) BeginDrain() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.draining = true
	m.notifyLocked()
}

// MarkServing is called only after startup and a successful business-listener
// bind. The control listener can be queried earlier but must report unready.
func (m *MigrationRuntime) MarkServing() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.serving = true
	m.notifyLocked()
}

func (m *MigrationRuntime) resume() {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.draining = false
	m.notifyLocked()
}

// RegisterTaskProbe adds explicitly identified partial coverage. A probe must
// be concurrency-safe, nonblocking, and return actual queue/worker statistics.
func (m *MigrationRuntime) RegisterTaskProbe(name string, probe func() MigrationTaskStats) {
	if name == "" || probe == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.taskProbes[name] = probe
}

func (m *MigrationRuntime) Snapshot() MigrationRuntimeSnapshot {
	m.mu.Lock()
	status := MigrationRuntimeSnapshot{
		InstanceID: RequestIDPrefix(), Draining: m.draining, Serving: m.serving,
		ActiveRequests: m.requests, ActiveHijackedConnections: m.hijacked,
		Ingress:                  m.ingressSnapshotLocked(),
		BackgroundTaskCoverage:   "unavailable",
		BackgroundWorkersEnabled: MigrationBackgroundWorkersEnabled(),
	}
	probes := make(map[string]func() MigrationTaskStats, len(m.taskProbes))
	for name, probe := range m.taskProbes {
		probes[name] = probe
	}
	m.mu.Unlock()
	if len(probes) > 0 {
		status.BackgroundTaskCoverage = "partial"
		status.Tasks = make(map[string]MigrationTaskStats, len(probes))
		for name, probe := range probes {
			status.Tasks[name] = probe()
		}
	}
	status.Readiness = migrationReadiness()
	status.Ready = status.Serving && !status.Draining && !status.Ingress.Paused
	for _, ready := range status.Readiness {
		status.Ready = status.Ready && ready
	}
	return status
}

var migrationControlRegistry = struct {
	sync.Mutex
	handlers  map[string]http.Handler
	readiness map[string]func() error
}{handlers: make(map[string]http.Handler), readiness: make(map[string]func() error)}

// RegisterMigrationReadinessProbe reports actual component readiness. The
// journal probe must include successful wiring at real gateway completion
// sites, not just the presence of a directory or worker. Probes must be safe
// to call while data access is paused and must not issue writes.
func RegisterMigrationReadinessProbe(name string, probe func() error) {
	if name == "" || probe == nil {
		panic("migration readiness requires a named probe")
	}
	migrationControlRegistry.Lock()
	defer migrationControlRegistry.Unlock()
	if _, exists := migrationControlRegistry.readiness[name]; exists {
		panic("migration readiness probe already registered")
	}
	migrationControlRegistry.readiness[name] = probe
}

func migrationReadiness() map[string]bool {
	readiness := map[string]bool{"postgres": false, "redis": false}
	if MigrationQuietHandoverEnabled() {
		readiness["writer_coverage"] = MigrationWriterCoverageReady() == nil
	} else {
		readiness["usage_journal"] = false
	}
	migrationControlRegistry.Lock()
	probes := make(map[string]func() error, len(migrationControlRegistry.readiness))
	for name, probe := range migrationControlRegistry.readiness {
		probes[name] = probe
	}
	migrationControlRegistry.Unlock()
	for name, probe := range probes {
		readiness[name] = probe() == nil
	}
	return readiness
}

// RegisterMigrationControlHandler registers a component-specific private
// subtree during application initialization, before main starts the listener.
// Only /data/ subtrees are accepted, keeping core drain/status routes owned by
// this runtime. Authentication wraps both core and registered handlers.
func RegisterMigrationControlHandler(prefix string, handler http.Handler) {
	if !strings.HasPrefix(prefix, "/data/") || !strings.HasSuffix(prefix, "/") || handler == nil {
		panic("migration control handlers require a /data/ subtree")
	}
	migrationControlRegistry.Lock()
	defer migrationControlRegistry.Unlock()
	if _, exists := migrationControlRegistry.handlers[prefix]; exists {
		panic("migration control subtree already registered")
	}
	migrationControlRegistry.handlers[prefix] = handler
}

// WrapHandler leaves the complete response-writer interface set intact, so SSE
// flushing and HTTP/1 WebSocket hijacking retain their original behavior.
// Draining changes readiness only: the ingress must route new traffic away.
func (m *MigrationRuntime) WrapHandler(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !m.admitMigrationRequest(r.Context()) {
			return
		}
		defer func() {
			m.mu.Lock()
			defer m.mu.Unlock()
			m.requests--
			m.notifyLocked()
		}()
		wrapped := httpsnoop.Wrap(w, httpsnoop.Hooks{
			Hijack: func(next httpsnoop.HijackFunc) httpsnoop.HijackFunc {
				return func() (net.Conn, *bufio.ReadWriter, error) {
					conn, rw, err := next()
					if err != nil {
						return conn, rw, err
					}
					m.mu.Lock()
					m.hijacked++
					m.notifyLocked()
					m.mu.Unlock()
					return &migrationTrackedConn{Conn: conn, onClose: func() {
						m.mu.Lock()
						defer m.mu.Unlock()
						m.hijacked--
						m.notifyLocked()
					}}, rw, nil
				}
			},
		})
		next.ServeHTTP(wrapped, r)
	})
}

type migrationTrackedConn struct {
	net.Conn
	once    sync.Once
	onClose func()
}

func (c *migrationTrackedConn) Close() error {
	err := c.Conn.Close()
	c.once.Do(c.onClose)
	return err
}

// WaitForRequests includes hijacked WebSocket transports that net/http's
// Shutdown does not wait for. It is not a background-writer drain barrier.
func (m *MigrationRuntime) WaitForRequests(ctx context.Context) error {
	for {
		m.mu.Lock()
		idle := m.requests == 0 && m.hijacked == 0
		changed := m.changed
		m.mu.Unlock()
		if idle {
			return nil
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-changed:
		}
	}
}

// ControlHandler is served on a separately bound loopback listener, never on
// the public API router. Every operation, including status, requires its token.
func (m *MigrationRuntime) ControlHandler(token string) http.Handler {
	mux := http.NewServeMux()
	mux.Handle("/data/ingress/", m.ingressControlHandler())
	migrationControlRegistry.Lock()
	for prefix, handler := range migrationControlRegistry.handlers {
		mux.Handle(prefix, handler)
	}
	migrationControlRegistry.Unlock()
	mux.HandleFunc("GET /status", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(m.Snapshot())
	})
	mux.HandleFunc("GET /ready", func(w http.ResponseWriter, _ *http.Request) {
		if !m.Snapshot().Ready {
			http.Error(w, "migration prerequisites are not ready or instance is draining", http.StatusServiceUnavailable)
			return
		}
		_, _ = w.Write([]byte("ready\n"))
	})
	mux.HandleFunc("POST /drain", func(w http.ResponseWriter, _ *http.Request) {
		m.BeginDrain()
		_, _ = w.Write([]byte("draining; route new requests away before stopping\n"))
	})
	mux.HandleFunc("POST /resume", func(w http.ResponseWriter, _ *http.Request) {
		m.resume()
		_, _ = w.Write([]byte("drain cleared; check /ready for component prerequisites\n"))
	})
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		if token == "" || subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+token)) != 1 {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		mux.ServeHTTP(w, r)
	})
}

func (m *MigrationRuntime) StartControl(cfg MigrationRuntimeConfig) (*http.Server, error) {
	if err := validateMigrationControlConfig(cfg); err != nil {
		return nil, err
	}
	listener, err := net.Listen("tcp", cfg.ListenAddress)
	if err != nil {
		return nil, fmt.Errorf("listen for migration control: %w", err)
	}
	server := &http.Server{
		Handler:           m.ControlHandler(cfg.Token),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second, WriteTimeout: 10 * time.Second,
		IdleTimeout: 30 * time.Second,
	}
	go func() { _ = server.Serve(listener) }()
	return server, nil
}
