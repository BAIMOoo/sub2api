package repository

import (
	"context"
	"errors"
	"fmt"
	"net"
	"sort"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/redis/go-redis/v9"
)

// MigrationRedisRebuildPlan must be installed after all configured consumers
// have registered. AssertQuiescent must prove the caller owns closed global
// admission AND zero admitted HTTP/WebSocket/background cache readers/writers.
// A Redis command barrier alone cannot protect process-local L1 caches.
type MigrationRedisRebuildPlan struct {
	RequiredSubscriptions []string
	CacheRebuilders       map[string]func(context.Context) error
	AssertQuiescent       func() error
}

type MigrationRedisConsumerStatus struct {
	Name       string `json:"name"`
	Channel    string `json:"channel"`
	Generation uint64 `json:"generation"`
}

type migrationRedisRebuildState struct {
	consumers map[string]*migrationRedisConsumer // guarded by opMu
	plan      *MigrationRedisRebuildPlan
	stopped   bool
	completed uint64
	changed   chan struct{}
}

type migrationRedisConsumer struct {
	spec   service.MigrationRedisSubscription
	ctx    context.Context
	worker *migrationRedisConsumerWorker
	ack    uint64 // guarded by opMu
}

type migrationRedisConsumerWorker struct {
	pubsub *redis.PubSub
	cancel context.CancelFunc
	done   chan struct{}
}

// The v9 pool can dial on a background context, losing the command's context
// token. During reconstruction only, allow those physical dials while normal
// ProcessHook/ProcessPipelineHook admission stays closed. Track them separately
// and drain them before acknowledging rebuild or allowing another switch.
func (h *MigrationRedisHandover) acquireDial(ctx context.Context) (context.Context, func(), error) {
	h.mu.Lock()
	if h.rebuildDialOpen && !h.closed {
		if h.rebuildDials == 0 {
			h.rebuildDialsIdle = make(chan struct{})
		}
		h.rebuildDials++
		h.mu.Unlock()
		return ctx, func() {
			h.mu.Lock()
			h.rebuildDials--
			if h.rebuildDials == 0 {
				close(h.rebuildDialsIdle)
			}
			h.mu.Unlock()
		}, nil
	}
	h.mu.Unlock()
	return h.acquire(ctx)
}

func (h *MigrationRedisHandover) finishRebuildDials(ctx context.Context) error {
	h.mu.Lock()
	h.rebuildDialOpen = false
	active, idle := h.rebuildDials, h.rebuildDialsIdle
	h.mu.Unlock()
	if active == 0 {
		return nil
	}
	select {
	case <-idle:
		return nil
	case <-ctx.Done():
		return fmt.Errorf("migration Redis reconstruction dials did not drain: %w", ctx.Err())
	}
}

func (h *MigrationRedisHandover) notifyConsumersLocked() {
	if h.rebuild.changed != nil {
		close(h.rebuild.changed)
	}
	h.rebuild.changed = make(chan struct{})
}

func (h *MigrationRedisHandover) SubscribeMigration(ctx context.Context, spec service.MigrationRedisSubscription) (<-chan error, error) {
	if ctx == nil || strings.TrimSpace(spec.Name) == "" || strings.TrimSpace(spec.Channel) == "" || spec.Handle == nil {
		return nil, errors.New("migration Redis consumer requires context, name, channel and handler")
	}
	h.opMu.Lock()
	if h.rebuild.consumers == nil {
		h.rebuild.consumers = make(map[string]*migrationRedisConsumer)
	}
	if _, exists := h.rebuild.consumers[spec.Name]; exists {
		h.opMu.Unlock()
		return nil, fmt.Errorf("migration Redis consumer %q already registered", spec.Name)
	}
	if h.rebuild.plan != nil {
		found := false
		for _, name := range h.rebuild.plan.RequiredSubscriptions {
			found = found || name == spec.Name
		}
		if !found {
			h.opMu.Unlock()
			return nil, fmt.Errorf("migration Redis consumer %q is outside the sealed manifest", spec.Name)
		}
	}
	if h.barrier.status().Paused || h.rebuild.stopped {
		h.opMu.Unlock()
		return nil, errors.New("migration Redis consumer registration is paused")
	}
	h.mu.Lock()
	closed, epoch := h.closed, h.epoch
	h.mu.Unlock()
	if closed {
		h.opMu.Unlock()
		return nil, net.ErrClosed
	}
	s := &migrationRedisConsumer{spec: spec, ctx: ctx}
	h.rebuild.consumers[spec.Name] = s
	if err := h.startConsumerLocked(ctx, s, epoch); err != nil {
		delete(h.rebuild.consumers, spec.Name)
		h.opMu.Unlock()
		return nil, err
	}
	h.notifyConsumersLocked()
	h.opMu.Unlock()
	done := make(chan error, 1)
	go h.superviseConsumer(s, done)
	return done, nil
}

func (h *MigrationRedisHandover) startConsumerLocked(setupCtx context.Context, s *migrationRedisConsumer, epoch uint64) error {
	if err := s.ctx.Err(); err != nil {
		return err
	}
	// ReceiveTimeout exposes the real SUBSCRIBE response. Channel's implicit
	// reconnect goroutine cannot prove which generation acknowledged a channel.
	setupCtx, cancelSetup := context.WithTimeout(setupCtx, 3*time.Second)
	defer cancelSetup()
	pubsub := h.client.Subscribe(setupCtx, s.spec.Channel)
	stopSetup := context.AfterFunc(setupCtx, func() { _ = pubsub.Close() })
	response, err := pubsub.ReceiveTimeout(setupCtx, 3*time.Second)
	if !stopSetup() {
		_ = pubsub.Close()
		return fmt.Errorf("migration Redis %s subscribe setup expired", s.spec.Name)
	}
	if err != nil {
		_ = pubsub.Close()
		return fmt.Errorf("migration Redis %s subscribe: %w", s.spec.Name, err)
	}
	ack, ok := response.(*redis.Subscription)
	if !ok || ack.Kind != "subscribe" || ack.Channel != s.spec.Channel || ack.Count != 1 {
		_ = pubsub.Close()
		return fmt.Errorf("migration Redis %s missing exact server subscribe acknowledgement", s.spec.Name)
	}
	h.mu.Lock()
	current, closed := h.epoch, h.closed
	h.mu.Unlock()
	if current != epoch || closed {
		_ = pubsub.Close()
		return errors.New("migration Redis generation changed before subscribe acknowledgement")
	}
	workerCtx, cancel := context.WithCancel(s.ctx)
	w := &migrationRedisConsumerWorker{pubsub: pubsub, cancel: cancel, done: make(chan struct{})}
	s.worker, s.ack = w, epoch
	if s.spec.Ready != nil {
		s.spec.Ready()
	}
	go func() {
		defer close(w.done)
		defer h.rebuildReady.Store(false)
		defer func() { _ = pubsub.Close() }()
		stop := context.AfterFunc(workerCtx, func() { _ = pubsub.Close() })
		defer stop()
		// New subscriptions ACK while paused, but their callbacks must not
		// mutate L1 or issue ordinary Redis commands during reconstruction.
		// Messages stay queued on the acknowledged target socket until Resume.
		release, err := h.barrier.acquire(workerCtx)
		if err != nil {
			return
		}
		release()
		for workerCtx.Err() == nil {
			message, err := pubsub.ReceiveTimeout(workerCtx, 250*time.Millisecond)
			if err != nil {
				if timeout, ok := err.(net.Error); ok && timeout.Timeout() && workerCtx.Err() == nil {
					continue
				}
				return
			}
			if value, ok := message.(*redis.Message); ok && value.Channel == s.spec.Channel {
				s.spec.Handle(workerCtx, value.Payload)
			}
		}
	}()
	return nil
}

func (h *MigrationRedisHandover) superviseConsumer(s *migrationRedisConsumer, result chan<- error) {
	defer close(result)
	for {
		h.opMu.Lock()
		w := s.worker
		h.opMu.Unlock()
		select {
		case <-s.ctx.Done():
			h.opMu.Lock()
			last := s.worker
			last.cancel()
			h.opMu.Unlock()
			<-last.done
			h.opMu.Lock()
			delete(h.rebuild.consumers, s.spec.Name)
			h.rebuild.completed = 0
			h.rebuildReady.Store(false)
			h.notifyConsumersLocked()
			h.opMu.Unlock()
			result <- s.ctx.Err()
			return
		case <-w.done:
		}
		h.opMu.Lock()
		if s.worker != w {
			h.opMu.Unlock()
			continue
		}
		s.ack = 0
		if h.rebuild.stopped || h.barrier.status().Paused {
			changed := h.rebuild.changed
			h.opMu.Unlock()
			select {
			case <-s.ctx.Done():
			case <-changed:
			}
			continue
		}
		h.rebuild.completed = 0 // a disconnect may have lost an invalidation
		h.rebuildReady.Store(false)
		h.mu.Lock()
		epoch, closed := h.epoch, h.closed
		h.mu.Unlock()
		if closed {
			h.opMu.Unlock()
			result <- net.ErrClosed
			return
		}
		err := h.startConsumerLocked(s.ctx, s, epoch)
		h.opMu.Unlock()
		if err != nil {
			timer := time.NewTimer(100 * time.Millisecond)
			select {
			case <-s.ctx.Done():
				timer.Stop()
			case <-timer.C:
			}
		}
	}
}

// stopConsumersLocked runs before closing the command barrier: PubSub receive
// bypasses command hooks and the driver may reconnect while holding its mutex.
// Cancel/close first, then wait for every old message handler to finish.
func (h *MigrationRedisHandover) stopConsumersLocked(ctx context.Context) error {
	h.rebuild.stopped = true
	h.rebuild.completed = 0
	h.rebuildReady.Store(false)
	for _, s := range h.rebuild.consumers {
		s.ack = 0
		if s.worker != nil {
			s.worker.cancel()
		}
	}
	for _, s := range h.rebuild.consumers {
		if s.worker == nil {
			continue
		}
		select {
		case <-s.worker.done:
		case <-ctx.Done():
			return fmt.Errorf("migration Redis consumer %s has not stopped: %w", s.spec.Name, ctx.Err())
		}
	}
	return nil
}

func (h *MigrationRedisHandover) ConfigureRebuild(plan MigrationRedisRebuildPlan) error {
	h.opMu.Lock()
	defer h.opMu.Unlock()
	if h.rebuild.plan != nil {
		return errors.New("migration Redis rebuild manifest already sealed")
	}
	if plan.AssertQuiescent == nil || len(plan.CacheRebuilders) == 0 {
		return errors.New("migration Redis rebuild requires global quiescence proof and explicit local cache rebuilders")
	}
	seen := make(map[string]bool)
	for _, name := range plan.RequiredSubscriptions {
		if name == "" || seen[name] || h.rebuild.consumers[name] == nil {
			return fmt.Errorf("migration Redis subscription manifest missing, empty or duplicate: %q", name)
		}
		seen[name] = true
	}
	if len(seen) != len(h.rebuild.consumers) {
		return errors.New("migration Redis subscription manifest does not cover every registered consumer")
	}
	copy := plan
	copy.RequiredSubscriptions = append([]string(nil), plan.RequiredSubscriptions...)
	copy.CacheRebuilders = make(map[string]func(context.Context) error, len(plan.CacheRebuilders))
	for name, fn := range plan.CacheRebuilders {
		if strings.TrimSpace(name) == "" || fn == nil {
			return errors.New("migration Redis local cache rebuilder requires name and function")
		}
		copy.CacheRebuilders[name] = fn
	}
	h.rebuild.plan = &copy
	return nil
}

// RebuildPaused performs only PubSub/local-cache reconstruction. The caller
// owns data replication, promotion, writer fencing and a global quiet gate.
// No normal Redis command is admitted until Resume succeeds.
func (h *MigrationRedisHandover) RebuildPaused(ctx context.Context, expectedEpoch uint64) (rebuildErr error) {
	h.opMu.Lock()
	defer h.opMu.Unlock()
	if h.rebuild.plan == nil {
		return ErrMigrationRedisRebuildUnwired
	}
	if err := h.barrier.requireDrained(); err != nil {
		return err
	}
	if err := h.rebuild.plan.AssertQuiescent(); err != nil {
		return fmt.Errorf("migration Redis global quiescence proof: %w", err)
	}
	h.mu.Lock()
	epoch, closed := h.epoch, h.closed
	h.mu.Unlock()
	if closed {
		return net.ErrClosed
	}
	if epoch != expectedEpoch {
		return errors.New("migration Redis rebuild epoch mismatch")
	}
	if !h.rebuild.stopped {
		return errors.New("migration Redis subscribers must be stopped before reconstruction")
	}
	h.rebuild.completed = 0
	h.mu.Lock()
	h.rebuildDialOpen = true
	h.mu.Unlock()
	defer func() {
		rebuildErr = errors.Join(rebuildErr, h.finishRebuildDials(ctx))
		if rebuildErr != nil {
			h.rebuild.completed = 0
			h.rebuildReady.Store(false)
		}
	}()
	setupCtx := context.WithValue(ctx, migrationRedisOperationKey{}, h)
	for _, name := range h.rebuild.plan.RequiredSubscriptions {
		s := h.rebuild.consumers[name]
		if s == nil {
			return fmt.Errorf("migration Redis consumer %q disappeared", name)
		}
		if s.worker != nil {
			select {
			case <-s.worker.done:
			default:
				return fmt.Errorf("migration Redis consumer %q is still active", name)
			}
		}
		if err := h.startConsumerLocked(setupCtx, s, epoch); err != nil {
			return err
		}
	}
	names := make([]string, 0, len(h.rebuild.plan.CacheRebuilders))
	for name := range h.rebuild.plan.CacheRebuilders {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		if err := ctx.Err(); err != nil {
			return err
		}
		if err := h.rebuild.plan.CacheRebuilders[name](setupCtx); err != nil {
			return fmt.Errorf("migration Redis rebuild local cache %q: %w", name, err)
		}
	}
	if err := h.rebuild.plan.AssertQuiescent(); err != nil {
		return fmt.Errorf("migration Redis global quiescence lost: %w", err)
	}
	if err := h.finishRebuildDials(ctx); err != nil {
		return err
	}
	h.rebuild.completed = epoch
	h.notifyConsumersLocked()
	if err := h.readinessLocked(); err != nil {
		return err
	}
	h.rebuildReady.Store(true)
	return nil
}

func (h *MigrationRedisHandover) readinessLocked() error {
	if h.rebuild.plan == nil {
		return ErrMigrationRedisRebuildUnwired
	}
	h.mu.Lock()
	epoch, closed := h.epoch, h.closed
	h.mu.Unlock()
	if closed {
		return net.ErrClosed
	}
	if h.rebuild.completed != epoch {
		return errors.New("migration Redis current generation caches have not been reconstructed")
	}
	for _, name := range h.rebuild.plan.RequiredSubscriptions {
		s := h.rebuild.consumers[name]
		if s == nil || s.ack != epoch || s.worker == nil {
			return fmt.Errorf("migration Redis consumer %q has not acknowledged current generation", name)
		}
		select {
		case <-s.worker.done:
			return fmt.Errorf("migration Redis consumer %q disconnected", name)
		default:
		}
	}
	return nil
}

func (h *MigrationRedisHandover) ConsumerStatus() []MigrationRedisConsumerStatus {
	h.opMu.Lock()
	defer h.opMu.Unlock()
	result := make([]MigrationRedisConsumerStatus, 0, len(h.rebuild.consumers))
	for name, s := range h.rebuild.consumers {
		result = append(result, MigrationRedisConsumerStatus{Name: name, Channel: s.spec.Channel, Generation: s.ack})
	}
	sort.Slice(result, func(i, j int) bool { return result[i].Name < result[j].Name })
	return result
}
