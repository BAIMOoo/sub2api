package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/repository"
	"github.com/Wei-Shaw/sub2api/internal/securityaudit"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/redis/go-redis/v9"
)

// A controller holds one writer fence across separate private HTTP calls.
// Endpoint credentials are configured at startup only.
type migrationDataControls struct {
	redis           *repository.MigrationRedisHandover
	source, target  string
	plan            repository.MigrationRedisRebuildPlan
	initialized     atomic.Bool
	held            atomic.Bool
	writerEpoch     atomic.Uint64
	lease           *service.MigrationWriterQuietLease // serialized by migration transition
	redisQuiet      atomic.Bool
	backgroundEpoch atomic.Uint64
}

func provideMigrationDataControls(
	_ *migrationWriterControls,
	rdb *redis.Client,
	auth *service.APIKeyService,
	sub *service.SubscriptionService,
	channel *service.ChannelService,
	passthrough *service.ErrorPassthroughService,
	tls *service.TLSFingerprintProfileService,
	prompt *securityaudit.ConfigManager,
) (*migrationDataControls, error) {
	c := &migrationDataControls{}
	if !service.MigrationModeEnabled() {
		return c, nil
	}
	c.redis = repository.MigrationRedisHandoverForClient(rdb)
	if !service.MigrationQuietHandoverEnabled() {
		return c, nil
	}
	if c.redis == nil {
		return nil, errors.New("quiet migration requires MIGRATION_REDIS_ENABLED")
	}
	c.source = c.redis.SourceAddress()
	c.target = strings.TrimSpace(os.Getenv("MIGRATION_REDIS_TARGET_ADDR"))
	host, port, err := net.SplitHostPort(c.target)
	portNumber, portError := strconv.Atoi(port)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() || portError != nil || portNumber < 1 || portNumber > 65535 || c.target == c.source {
		return nil, errors.New("quiet migration requires a distinct loopback MIGRATION_REDIS_TARGET_ADDR")
	}
	required := []string{"channel", "error_passthrough", "tls_fingerprint", "prompt_audit"}
	if auth.MigrationAuthSubscriptionRequired() {
		required = append(required, "auth")
	}
	if sub.MigrationSubscriptionSubscriberRequired() {
		required = append(required, "subscription")
	}
	c.plan = repository.MigrationRedisRebuildPlan{
		RequiredSubscriptions: required,
		CacheRebuilders: map[string]func(context.Context) error{
			"auth":              auth.ClearMigrationAuthCache,
			"subscription":      sub.ClearMigrationSubscriptionCache,
			"channel":           channel.ClearMigrationChannelCache,
			"error_passthrough": passthrough.RebuildMigrationErrorCache,
			"tls_fingerprint":   tls.RebuildMigrationTLSCache,
			"prompt_audit":      prompt.RebuildMigrationSnapshot,
		},
		AssertQuiescent: service.MigrationDataQuiet,
	}
	service.RegisterMigrationReadinessProbe("redis", c.redisReady)
	service.RegisterMigrationReadinessProbe("quiet_controls", func() error {
		if !c.initialized.Load() {
			return errors.New("quiet migration controls are not initialized")
		}
		return nil
	})
	service.RegisterMigrationPostgresQuietGuard(func() error {
		if !c.redisQuiet.Load() || !c.redis.Status().Paused {
			return errors.New("Redis consumers and commands must be paused before PostgreSQL")
		}
		return nil
	})
	service.RegisterMigrationControlHandler("/data/writers/", http.HandlerFunc(c.writerControl))
	service.RegisterMigrationControlHandler("/data/redis/", http.HandlerFunc(c.redisControl))
	service.RegisterMigrationControlHandler("/data/background/", http.HandlerFunc(c.backgroundControl))
	return c, nil
}

func (c *migrationDataControls) backgroundStatus(w http.ResponseWriter) {
	_ = json.NewEncoder(w).Encode(struct {
		Epoch uint64 `json:"epoch"`
		service.MigrationBackgroundActivationSnapshot
	}{c.backgroundEpoch.Load(), service.MigrationBackgroundActivationStatus()})
}

// The independent runner must additionally prove that the old AWS application
// has stopped. A local process cannot establish retirement of that remote owner.
func (c *migrationDataControls) backgroundControl(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	unlock := service.LockMigrationTransition()
	defer unlock()
	op := strings.TrimPrefix(r.URL.Path, "/data/background/")
	if r.Method == http.MethodGet && op == "status" {
		c.backgroundStatus(w)
		return
	}
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	if op != "activate" {
		w.WriteHeader(http.StatusNotFound)
		return
	}
	epoch, endpoint, err := migrationControlRequest(w, r)
	if err != nil || endpoint != "" {
		http.Error(w, "invalid background control request", http.StatusBadRequest)
		return
	}
	if !c.initialized.Load() || c.held.Load() || epoch != c.backgroundEpoch.Load() || c.redis == nil || c.redis.Status().Address != c.target {
		http.Error(w, "background activation phase does not match", http.StatusConflict)
		return
	}
	if err = service.MigrationPostgresTargetReady(); err != nil {
		http.Error(w, "PostgreSQL must use the target authority", http.StatusConflict)
		return
	}
	if err = service.MigrationDataEndpointsReady(); err != nil {
		http.Error(w, "data endpoints must be ready before background activation", http.StatusConflict)
		return
	}
	err = service.ActivateMigrationBackgroundWorkers()
	c.backgroundEpoch.Add(1)
	if err != nil {
		http.Error(w, "background activation failed; inspect private status", http.StatusConflict)
		return
	}
	c.backgroundStatus(w)
}

func (c *migrationDataControls) redisReady() error {
	if c.redis == nil {
		return errors.New("migration Redis is not connected")
	}
	s := c.redis.Status()
	if s.Paused || s.Closed || !s.PubSubCacheReady {
		return errors.New("migration Redis is paused, closed or caches are unready")
	}
	return c.redis.Readiness()
}

// Initialize runs before public serving, after all consumer services start.
// The manifest must cover the actual configured subscribers.
func (c *migrationDataControls) Initialize(parent context.Context, m *service.MigrationRuntime) error {
	if !service.MigrationQuietHandoverEnabled() {
		return nil
	}
	if m == nil || c.redis == nil {
		return errors.New("quiet migration requires private runtime and Redis controls")
	}
	unlock := service.LockMigrationTransition()
	defer unlock()
	ctx, cancel := context.WithTimeout(parent, 15*time.Second)
	defer cancel()
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	for {
		if err := c.redis.ConfigureRebuild(c.plan); err == nil {
			break
		}
		select {
		case <-ctx.Done():
			return errors.New("migration Redis consumer manifest did not register completely")
		case <-ticker.C:
		}
	}
	ingress := m.PauseIngress()
	lease, err := service.BeginMigrationWriterQuiesce(ctx)
	if err != nil {
		return fmt.Errorf("initialize quiet writers: %w", err)
	}
	// Failed initialization never serves. Retain closed fences rather than
	// accepting traffic with partially reconstructed caches.
	if err = c.redis.Pause(ctx); err != nil {
		return err
	}
	c.redisQuiet.Store(true)
	if err = c.redis.RebuildPaused(ctx, c.redis.Status().Epoch); err != nil {
		return err
	}
	if err = c.redis.Resume(); err != nil {
		return err
	}
	c.redisQuiet.Store(false)
	if err = service.MigrationDataEndpointsReady(); err != nil {
		return err
	}
	lease.Resume()
	m.SetIngressResumeGuard(func() error {
		if c.held.Load() {
			return errors.New("migration writer fence is still held")
		}
		return service.MigrationDataEndpointsReady()
	})
	if err = m.ResumeIngress(ingress.Epoch); err != nil {
		return err
	}
	c.initialized.Store(true)
	return nil
}

func migrationControlRequest(w http.ResponseWriter, r *http.Request) (uint64, string, error) {
	var q struct {
		ExpectedEpoch *uint64 `json:"expected_epoch"`
		Endpoint      string  `json:"endpoint,omitempty"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1024))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&q); err != nil || q.ExpectedEpoch == nil {
		return 0, "", errors.New("expected_epoch is required")
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return 0, "", errors.New("invalid trailing request data")
	}
	return *q.ExpectedEpoch, q.Endpoint, nil
}
func (c *migrationDataControls) writerStatus(w http.ResponseWriter) {
	_ = json.NewEncoder(w).Encode(map[string]any{"epoch": c.writerEpoch.Load(), "held": c.held.Load(), "quiet": service.MigrationWritersQuiet() == nil})
}
func (c *migrationDataControls) writerControl(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	unlock := service.LockMigrationTransition()
	defer unlock()
	op := strings.TrimPrefix(r.URL.Path, "/data/writers/")
	if r.Method == http.MethodGet && op == "status" {
		c.writerStatus(w)
		return
	}
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	epoch, endpoint, err := migrationControlRequest(w, r)
	if err != nil || endpoint != "" {
		http.Error(w, "invalid writer control request", 400)
		return
	}
	if !c.initialized.Load() || epoch != c.writerEpoch.Load() {
		http.Error(w, "writer phase mismatch or uninitialized controller", 409)
		return
	}
	if err = service.MigrationIngressQuiet(); err != nil {
		http.Error(w, "closed and drained ingress is required", 409)
		return
	}
	switch op {
	case "pause":
		if !c.held.Load() {
			ctx, cancel := context.WithTimeout(r.Context(), 1500*time.Millisecond)
			defer cancel()
			lease, beginErr := service.BeginMigrationWriterQuiesce(ctx)
			if beginErr != nil {
				http.Error(w, "writers could not reach quiet state", 409)
				return
			}
			c.lease = lease
			c.held.Store(true)
			c.writerEpoch.Add(1)
		}
	case "resume":
		if c.held.Load() {
			if err = service.MigrationDataEndpointsReady(); err != nil {
				http.Error(w, "data endpoints must be ready before writer resume", 409)
				return
			}
			c.lease.Resume()
			c.lease = nil
			c.held.Store(false)
			c.writerEpoch.Add(1)
		}
	default:
		w.WriteHeader(http.StatusNotFound)
		return
	}
	c.writerStatus(w)
}
func (c *migrationDataControls) redisStatus(w http.ResponseWriter) {
	_ = json.NewEncoder(w).Encode(struct {
		repository.MigrationRedisStatus
		Consumers []repository.MigrationRedisConsumerStatus `json:"consumers"`
		Quiet     bool                                      `json:"quiet"`
	}{c.redis.Status(), c.redis.ConsumerStatus(), c.redisQuiet.Load()})
}
func (c *migrationDataControls) redisControl(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	unlock := service.LockMigrationTransition()
	defer unlock()
	op := strings.TrimPrefix(r.URL.Path, "/data/redis/")
	if r.Method == http.MethodGet && op == "status" {
		c.redisStatus(w)
		return
	}
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	epoch, endpoint, err := migrationControlRequest(w, r)
	if err != nil {
		http.Error(w, "invalid Redis control request", 400)
		return
	}
	if !c.initialized.Load() || epoch != c.redis.Status().Epoch {
		http.Error(w, "Redis phase mismatch or uninitialized controller", 409)
		return
	}
	if err = service.MigrationDataQuiet(); err != nil {
		http.Error(w, "closed ingress and committed writer fence are required", 409)
		return
	}
	if op != "switch" && endpoint != "" {
		http.Error(w, "unexpected endpoint", 400)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 1500*time.Millisecond)
	defer cancel()
	switch op {
	case "pause":
		c.redisQuiet.Store(false)
		err = c.redis.Pause(ctx)
		if err == nil {
			c.redisQuiet.Store(true)
		}
	case "switch":
		if !c.redisQuiet.Load() {
			http.Error(w, "Redis pause did not complete", 409)
			return
		}
		address := map[string]string{"source": c.source, "target": c.target}[endpoint]
		if address == "" {
			http.Error(w, "unknown configured endpoint", 400)
			return
		}
		_, err = c.redis.SwitchPaused(address, epoch)
	case "rebuild":
		err = c.redis.RebuildPaused(ctx, epoch)
	case "resume":
		err = c.redis.Resume()
		if err == nil {
			c.redisQuiet.Store(false)
		}
	default:
		w.WriteHeader(http.StatusNotFound)
		return
	}
	if err != nil {
		http.Error(w, "Redis control did not complete; admission remains closed", 409)
		return
	}
	c.redisStatus(w)
}
