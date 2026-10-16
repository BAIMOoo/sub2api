package repository

import (
	"context"
	"errors"
	"fmt"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/redis/go-redis/v9"
	"github.com/redis/go-redis/v9/maintnotifications"
)

var ErrMigrationRedisRebuildUnwired = errors.New("migration Redis PubSub coverage and local cache rebuild are not connected")

type migrationRedisOperationKey struct{}

// MigrationRedisHandover routes one fixed Client through explicit endpoint
// generations. InitRedis constructs it only with MIGRATION_REDIS_ENABLED=true.
// Credentials/DB/protocol are copied once and never changed during handover.
// TLS, opaque transports, client-side caching, WATCH/Conn/blocking commands and
// consumers bypassing normal command hooks need separate verified integration.
type MigrationRedisHandover struct {
	opMu             sync.Mutex
	mu               sync.Mutex
	barrier          migrationBarrier
	epoch            uint64
	addr             string
	sourceAddr       string
	closed           bool
	conns            map[*migrationRedisConn]struct{}
	client           *redis.Client
	dial             func(context.Context, string, string) (net.Conn, error)
	rebuild          migrationRedisRebuildState
	rebuildReady     atomic.Bool
	rebuildDialOpen  bool
	rebuildDials     int
	rebuildDialsIdle chan struct{}
}

var migrationRedisClients sync.Map // *redis.Client -> *MigrationRedisHandover

func MigrationRedisHandoverForClient(client *redis.Client) *MigrationRedisHandover {
	value, ok := migrationRedisClients.Load(client)
	if !ok {
		return nil
	}
	return value.(*MigrationRedisHandover)
}

func (h *MigrationRedisHandover) SourceAddress() string { return h.sourceAddr }

type MigrationRedisStatus struct {
	Epoch            uint64 `json:"epoch"`
	Address          string `json:"address"`
	Paused           bool   `json:"paused"`
	ActiveOperations int    `json:"active_operations"`
	TrackedSockets   int    `json:"tracked_sockets"`
	Closed           bool   `json:"closed"`
	PubSubCacheReady bool   `json:"pubsub_cache_ready"`
}

func validateMigrationRedisAddress(addr string) error {
	host, port, err := net.SplitHostPort(addr)
	if err != nil || host == "" || port == "" || strings.ContainsAny(host, "@/?#") {
		return errors.New("migration Redis endpoint requires a host:port without credentials or URL parameters")
	}
	return nil
}

func NewMigrationRedisClient(options *redis.Options) (*redis.Client, *MigrationRedisHandover, error) {
	if options == nil {
		return nil, nil, errors.New("migration Redis options are required")
	}
	return newMigrationRedisClient(options, options.Addr)
}

func newMigrationRedisClient(options *redis.Options, initialAddress string) (*redis.Client, *MigrationRedisHandover, error) {
	if options == nil {
		return nil, nil, errors.New("migration Redis options are required")
	}
	if err := validateMigrationRedisAddress(options.Addr); err != nil {
		return nil, nil, err
	}
	if err := validateMigrationRedisAddress(initialAddress); err != nil {
		return nil, nil, err
	}
	if options.Network != "" && options.Network != "tcp" {
		return nil, nil, errors.New("migration Redis foundation currently requires plain TCP")
	}
	if options.TLSConfig != nil {
		return nil, nil, errors.New("migration Redis TLS pool replacement is not verified")
	}
	if options.CredentialsProvider != nil || options.CredentialsProviderContext != nil || options.StreamingCredentialsProvider != nil {
		return nil, nil, errors.New("migration Redis requires static identical credentials across generations")
	}
	if options.ClientSideCache != nil || options.ClientSideCacheConfig != nil {
		return nil, nil, errors.New("migration Redis client-side cache reconstruction is not verified")
	}
	if options.MaintNotificationsConfig != nil && options.MaintNotificationsConfig.Mode != maintnotifications.ModeDisabled {
		return nil, nil, errors.New("migration Redis requires automatic endpoint maintenance to be disabled")
	}
	// Copy before redis.NewClient applies defaults. Mutation of the caller's
	// source options cannot race authentication against a changed endpoint.
	copy := *options
	// A separate driver-directed endpoint handoff must never bypass our epoch.
	copy.MaintNotificationsConfig = &maintnotifications.Config{Mode: maintnotifications.ModeDisabled}
	if copy.DialTimeout == 0 {
		copy.DialTimeout = 5 * time.Second
	}
	baseDial := copy.Dialer
	if baseDial == nil {
		baseDial = redis.NewDialer(&copy)
	}
	h := &MigrationRedisHandover{epoch: 1, addr: initialAddress, sourceAddr: copy.Addr, conns: make(map[*migrationRedisConn]struct{}), dial: baseDial}
	copy.Dialer = h.dialEpoch
	client := redis.NewClient(&copy)
	h.client = client
	client.AddHook(h)
	if err := service.RegisterMigrationRedisSubscriptionManager(client, h); err != nil {
		_ = client.Close()
		return nil, nil, err
	}
	migrationRedisClients.Store(client, h)
	return client, h, nil
}

func (h *MigrationRedisHandover) acquire(ctx context.Context) (context.Context, func(), error) {
	if owner, _ := ctx.Value(migrationRedisOperationKey{}).(*MigrationRedisHandover); owner == h {
		// go-redis invokes hooks recursively for HELLO/AUTH/init pipelines.
		// Reusing the outer lease avoids deadlocking a pause against itself.
		return ctx, func() {}, nil
	}
	release, err := h.barrier.acquire(ctx)
	if err != nil {
		return nil, nil, err
	}
	h.mu.Lock()
	closed := h.closed
	h.mu.Unlock()
	if closed {
		release()
		return nil, nil, net.ErrClosed
	}
	return context.WithValue(ctx, migrationRedisOperationKey{}, h), release, nil
}

func (h *MigrationRedisHandover) DialHook(next redis.DialHook) redis.DialHook { return next }

func (h *MigrationRedisHandover) ProcessHook(next redis.ProcessHook) redis.ProcessHook {
	return func(ctx context.Context, cmd redis.Cmder) error {
		operationCtx, release, err := h.acquire(ctx)
		if err != nil {
			return err
		}
		defer release()
		return next(operationCtx, cmd)
	}
}

func (h *MigrationRedisHandover) ProcessPipelineHook(next redis.ProcessPipelineHook) redis.ProcessPipelineHook {
	return func(ctx context.Context, commands []redis.Cmder) error {
		operationCtx, release, err := h.acquire(ctx)
		if err != nil {
			return err
		}
		defer release()
		// The driver uses this hook for ordinary and transactional pipelines.
		// Keep the lease until the complete result (including errors) returns.
		return next(operationCtx, commands)
	}
}

func (h *MigrationRedisHandover) dialEpoch(ctx context.Context, network, _ string) (net.Conn, error) {
	operationCtx, release, err := h.acquireDial(ctx)
	if err != nil {
		return nil, err
	}
	defer release()
	h.mu.Lock()
	epoch, addr, closed := h.epoch, h.addr, h.closed
	h.mu.Unlock()
	if closed {
		return nil, net.ErrClosed
	}
	conn, err := h.dial(operationCtx, network, addr)
	if err != nil {
		return nil, err
	}
	sysConn, ok := conn.(syscall.Conn)
	if !ok {
		_ = conn.Close()
		return nil, errors.New("migration Redis dialer returned an opaque transport; pool retirement is not verified")
	}
	tracked := &migrationRedisConn{Conn: conn, syscallConn: sysConn, owner: h, epoch: epoch}
	h.mu.Lock()
	// Background MinIdleConns dials must be registered against the generation
	// again after connect. Never publish a socket created for an earlier end.
	if h.closed || h.epoch != epoch || h.addr != addr {
		h.mu.Unlock()
		_ = conn.Close()
		return nil, errors.New("migration Redis generation changed while dialing")
	}
	h.conns[tracked] = struct{}{}
	h.mu.Unlock()
	return tracked, nil
}

func (h *MigrationRedisHandover) Pause(ctx context.Context) error {
	h.opMu.Lock()
	defer h.opMu.Unlock()
	consumerErr := h.stopConsumersLocked(ctx)
	barrierErr := h.barrier.pause(ctx)
	return errors.Join(consumerErr, barrierErr)
}

// SwitchPaused changes only routing. Replication promotion, old-writer fencing,
// final synchronization, keyspace validation, and lease/time semantics remain
// orchestrator responsibilities. An error keeps admission paused.
func (h *MigrationRedisHandover) SwitchPaused(target string, expectedEpoch uint64) (uint64, error) {
	if err := validateMigrationRedisAddress(target); err != nil {
		return 0, err
	}
	h.opMu.Lock()
	defer h.opMu.Unlock()
	var sockets []*migrationRedisConn
	var nextEpoch uint64
	for name, consumer := range h.rebuild.consumers {
		if consumer.worker != nil {
			select {
			case <-consumer.worker.done:
			default:
				return 0, fmt.Errorf("migration Redis consumer %q is not drained", name)
			}
		}
	}
	err := h.barrier.whileDrained(func() error {
		h.mu.Lock()
		defer h.mu.Unlock()
		if h.closed {
			return net.ErrClosed
		}
		if h.rebuildDials != 0 {
			return errors.New("migration Redis reconstruction dials have not drained")
		}
		if h.epoch != expectedEpoch {
			return fmt.Errorf("migration Redis epoch mismatch: current=%d expected=%d", h.epoch, expectedEpoch)
		}
		if h.addr == target {
			nextEpoch = h.epoch
			return nil
		}
		h.epoch++
		h.addr = target
		h.rebuild.completed = 0
		h.rebuildReady.Store(false)
		nextEpoch = h.epoch
		for socket := range h.conns {
			sockets = append(sockets, socket)
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	// Admission remains paused, and opMu prevents a concurrent Resume until
	// every old pooled/PubSub socket is closed. Network I/O is outside the
	// barrier lock. The public Client object itself stays unchanged.
	var firstErr error
	for _, socket := range sockets {
		if err := socket.Close(); err != nil && !errors.Is(err, net.ErrClosed) && firstErr == nil {
			firstErr = err
		}
	}
	return nextEpoch, firstErr
}

func (h *MigrationRedisHandover) Resume() error {
	h.opMu.Lock()
	defer h.opMu.Unlock()
	h.mu.Lock()
	closed := h.closed
	h.mu.Unlock()
	if closed {
		return net.ErrClosed
	}
	if h.rebuild.plan != nil {
		if err := h.readinessLocked(); err != nil {
			return err
		}
	}
	h.rebuild.stopped = false
	h.barrier.resume()
	h.notifyConsumersLocked()
	return nil
}

func (h *MigrationRedisHandover) Status() MigrationRedisStatus {
	barrier := h.barrier.status()
	h.mu.Lock()
	defer h.mu.Unlock()
	return MigrationRedisStatus{Epoch: h.epoch, Address: h.addr, Paused: barrier.Paused,
		ActiveOperations: barrier.Active, TrackedSockets: len(h.conns), Closed: h.closed,
		PubSubCacheReady: h.rebuildReady.Load()}
}

// Readiness deliberately stays false until all subscription consumers and
// their process-local caches have a verified reconstruction barrier. A Ping
// succeeding against the new server does not prove that invalidations survived.
func (h *MigrationRedisHandover) Readiness() error {
	h.opMu.Lock()
	defer h.opMu.Unlock()
	return h.readinessLocked()
}

func (h *MigrationRedisHandover) Close() error {
	h.opMu.Lock()
	defer h.opMu.Unlock()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	consumerErr := h.stopConsumersLocked(ctx)
	h.mu.Lock()
	h.closed = true
	h.mu.Unlock()
	h.barrier.resume()
	h.notifyConsumersLocked()
	migrationRedisClients.Delete(h.client)
	service.UnregisterMigrationRedisSubscriptionManager(h.client)
	return errors.Join(consumerErr, h.client.Close())
}

type migrationRedisConn struct {
	net.Conn
	syscallConn syscall.Conn
	owner       *MigrationRedisHandover
	epoch       uint64
	once        sync.Once
	closeErr    error
}

func (c *migrationRedisConn) Close() error {
	c.once.Do(func() {
		c.closeErr = c.Conn.Close()
		c.owner.mu.Lock()
		delete(c.owner.conns, c)
		c.owner.mu.Unlock()
	})
	return c.closeErr
}

// Preserve the driver's real socket health check. Closed old pool entries are
// evicted during checkout, before a business command is sent, even with driver
// retries disabled. An opaque wrapper would hide this check and expose errors.
func (c *migrationRedisConn) SyscallConn() (syscall.RawConn, error) {
	c.owner.mu.Lock()
	stale := c.owner.closed || c.epoch != c.owner.epoch
	c.owner.mu.Unlock()
	if stale {
		return nil, net.ErrClosed
	}
	return c.syscallConn.SyscallConn()
}
