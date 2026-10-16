package service

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
)

// The list is deliberately exhaustive at the component boundary. A zero-length
// queue from one pool never substitutes for a missing producer/writer fence.
// Registration does not claim that unpatched processes on another host comply.
var migrationRequiredWriters = []string{
	"timing_wheel", "scheduled_writers", "plugins", "prompt_audit", "prompt_config",
	"auth_outbox", "ops_runtime", "usage_records", "billing_cache", "content_moderation", "detached_writers", "deferred_last_used",
	"audit_log", "ops_ingress_reject", "ops_system_log",
}

type migrationWriterBinding struct {
	probe func() MigrationTaskStats
	begin func(context.Context) (func(), error)
}

var migrationWriterRegistry = struct {
	sync.Mutex
	bindings map[string]migrationWriterBinding
	leased   bool
	quiet    bool
}{bindings: make(map[string]migrationWriterBinding)}

// RegisterMigrationWriterQuiescer is startup-only, opt-in application wiring.
// begin must fence NEW work and await already accepted work without permanently
// stopping request-derived pools. It returns an idempotent resume even on error
// if a fence was acquired. The probe must be nonblocking and concurrency-safe.
func RegisterMigrationWriterQuiescer(name string, probe func() MigrationTaskStats, begin func(context.Context) (func(), error)) {
	if name == "" || probe == nil || begin == nil {
		panic("migration writer binding requires a name, probe and reversible quiescer")
	}
	migrationWriterRegistry.Lock()
	defer migrationWriterRegistry.Unlock()
	if _, exists := migrationWriterRegistry.bindings[name]; exists || migrationWriterRegistry.leased {
		panic("migration writer binding duplicated or registered during a quiet lease")
	}
	migrationWriterRegistry.bindings[name] = migrationWriterBinding{probe, begin}
}

// Coverage readiness validates wiring only. It does not assert that producers
// are currently fenced, ingress is closed, or data handover is safe.
func MigrationWriterCoverageReady() error {
	migrationWriterRegistry.Lock()
	defer migrationWriterRegistry.Unlock()
	for _, name := range migrationRequiredWriters {
		if _, exists := migrationWriterRegistry.bindings[name]; !exists {
			return fmt.Errorf("migration writer coverage missing: %s", name)
		}
	}
	return nil
}

func MigrationWritersQuiet() error {
	migrationWriterRegistry.Lock()
	if !migrationWriterRegistry.leased || !migrationWriterRegistry.quiet {
		migrationWriterRegistry.Unlock()
		return errors.New("migration writer quiet lease is not established")
	}
	bindings := make(map[string]migrationWriterBinding, len(migrationWriterRegistry.bindings))
	for name, binding := range migrationWriterRegistry.bindings {
		bindings[name] = binding
	}
	migrationWriterRegistry.Unlock()
	for name, binding := range bindings {
		stats, err := migrationProbeWriter(binding.probe)
		if err != nil {
			return fmt.Errorf("migration writer %s probe failed: %w", name, err)
		}
		if stats.Active != 0 || stats.Pending != 0 {
			return fmt.Errorf("migration writer %s still has active=%d pending=%d", name, stats.Active, stats.Pending)
		}
	}
	migrationWriterRegistry.Lock()
	defer migrationWriterRegistry.Unlock()
	if !migrationWriterRegistry.leased || !migrationWriterRegistry.quiet {
		return errors.New("migration writer quiet lease was released")
	}
	return nil
}

func MigrationRequiredWriterNames() []string {
	return append([]string(nil), migrationRequiredWriters...)
}

func MigrationWriterProbes() map[string]func() MigrationTaskStats {
	migrationWriterRegistry.Lock()
	defer migrationWriterRegistry.Unlock()
	probes := make(map[string]func() MigrationTaskStats, len(migrationWriterRegistry.bindings))
	for name, binding := range migrationWriterRegistry.bindings {
		probes[name] = binding.probe
	}
	return probes
}

// MigrationWriterQuietLease is valid only while ingress is independently
// closed and this lease remains held. Resume does not open HTTP admission.
// This is a data-handover fence, not a safe_to_stop assertion.
type MigrationWriterQuietLease struct {
	once    sync.Once
	resumes []func()
}

func (lease *MigrationWriterQuietLease) Resume() {
	if lease == nil {
		return
	}
	lease.once.Do(func() {
		var firstPanic any
		for index := len(lease.resumes) - 1; index >= 0; index-- {
			func() {
				defer func() {
					if value := recover(); value != nil && firstPanic == nil {
						firstPanic = value
					}
				}()
				lease.resumes[index]()
			}()
		}
		migrationWriterRegistry.Lock()
		migrationWriterRegistry.leased = false
		migrationWriterRegistry.quiet = false
		migrationWriterRegistry.Unlock()
		if firstPanic != nil {
			panic(firstPanic)
		}
	})
}

// BeginMigrationWriterQuiesce requires every named component, fences producers
// before draining consumers, then rechecks all accepted-task counters. Any
// failure resumes every acquired fence. Callers must also prove ingress quiet
// before and after this call and always defer lease.Resume on success.
func BeginMigrationWriterQuiesce(ctx context.Context) (*MigrationWriterQuietLease, error) {
	migrationWriterRegistry.Lock()
	migrationStartedBackgroundWorkers.Lock()
	activated := migrationStartedBackgroundWorkers.activated
	migrationStartedBackgroundWorkers.Unlock()
	if activated {
		migrationWriterRegistry.Unlock()
		return nil, errors.New("migration quiet handover is unavailable after background activation")
	}
	if migrationWriterRegistry.leased {
		migrationWriterRegistry.Unlock()
		return nil, errors.New("migration writer quiet lease is already held")
	}
	bindings := make(map[string]migrationWriterBinding, len(migrationWriterRegistry.bindings))
	for name, binding := range migrationWriterRegistry.bindings {
		bindings[name] = binding
	}
	missing := []string{}
	for _, name := range migrationRequiredWriters {
		if _, exists := bindings[name]; !exists {
			missing = append(missing, name)
		}
	}
	if len(missing) > 0 {
		migrationWriterRegistry.Unlock()
		return nil, fmt.Errorf("migration writer coverage missing: %s", strings.Join(missing, ", "))
	}
	migrationWriterRegistry.leased = true
	migrationWriterRegistry.Unlock()
	lease := &MigrationWriterQuietLease{}
	fail := func(err error) (*MigrationWriterQuietLease, error) {
		lease.Resume()
		return nil, err
	}
	names := append([]string(nil), migrationRequiredWriters...)
	// Additional explicitly registered writers cannot disappear from coverage.
	extra := []string{}
	for name := range bindings {
		found := false
		for _, required := range migrationRequiredWriters {
			found = found || required == name
		}
		if !found {
			extra = append(extra, name)
		}
	}
	sort.Strings(extra)
	names = append(names, extra...)
	for _, name := range names {
		if err := ctx.Err(); err != nil {
			return fail(err)
		}
		resume, err := migrationBeginWriter(ctx, bindings[name].begin)
		if resume != nil {
			lease.resumes = append(lease.resumes, resume)
		}
		if err != nil {
			return fail(fmt.Errorf("migration writer %s is not quiet: %w", name, err))
		}
		if resume == nil {
			return fail(fmt.Errorf("migration writer %s did not return a reversible fence", name))
		}
	}
	for _, name := range names {
		stats, err := migrationProbeWriter(bindings[name].probe)
		if err != nil {
			return fail(fmt.Errorf("migration writer %s probe failed: %w", name, err))
		}
		if stats.Active != 0 || stats.Pending != 0 {
			return fail(fmt.Errorf("migration writer %s still has active=%d pending=%d", name, stats.Active, stats.Pending))
		}
	}
	if err := ctx.Err(); err != nil {
		return fail(err)
	}
	migrationWriterRegistry.Lock()
	migrationWriterRegistry.quiet = true
	migrationWriterRegistry.Unlock()
	return lease, nil
}

func migrationBeginWriter(ctx context.Context, begin func(context.Context) (func(), error)) (resume func(), err error) {
	defer func() {
		if value := recover(); value != nil {
			err = fmt.Errorf("quiescer panicked: %v", value)
		}
	}()
	return begin(ctx)
}

func migrationProbeWriter(probe func() MigrationTaskStats) (stats MigrationTaskStats, err error) {
	defer func() {
		if value := recover(); value != nil {
			err = fmt.Errorf("probe panicked: %v", value)
		}
	}()
	return probe(), nil
}

func RunMigrationWriterQuiesce(ctx context.Context) (*MigrationWriterQuietLease, error) {
	return BeginMigrationWriterQuiesce(ctx)
}

// migrationWorkCounter counts accepted tasks from admission through completion,
// including queued work and inline overflow fallbacks. Idle worker goroutines
// are not active tasks. Zero is observational; a separate producer fence is
// needed to keep it zero throughout data handover.
type migrationWorkCounter struct {
	mu      sync.Mutex
	changed chan struct{}
	stats   MigrationTaskStats
}

type migrationWorkTicket struct {
	owner   *migrationWorkCounter
	started bool
	done    bool
}

func (counter *migrationWorkCounter) notifyLocked() {
	if counter.changed != nil {
		close(counter.changed)
	}
	counter.changed = make(chan struct{})
}

func (counter *migrationWorkCounter) accept() *migrationWorkTicket {
	counter.mu.Lock()
	defer counter.mu.Unlock()
	counter.stats.Pending++
	counter.notifyLocked()
	return &migrationWorkTicket{owner: counter}
}

func (ticket *migrationWorkTicket) start() {
	if ticket == nil {
		return
	}
	counter := ticket.owner
	counter.mu.Lock()
	defer counter.mu.Unlock()
	if ticket.done || ticket.started {
		return
	}
	ticket.started = true
	counter.stats.Pending--
	counter.stats.Active++
	counter.notifyLocked()
}

func (ticket *migrationWorkTicket) finish(failed bool) {
	if ticket == nil {
		return
	}
	counter := ticket.owner
	counter.mu.Lock()
	defer counter.mu.Unlock()
	if ticket.done {
		return
	}
	ticket.done = true
	if ticket.started {
		counter.stats.Active--
	} else {
		counter.stats.Pending--
	}
	if failed {
		counter.stats.Failed++
	}
	counter.notifyLocked()
}

func (counter *migrationWorkCounter) snapshot() MigrationTaskStats {
	counter.mu.Lock()
	defer counter.mu.Unlock()
	return counter.stats
}

func (counter *migrationWorkCounter) wait(ctx context.Context) error {
	for {
		counter.mu.Lock()
		idle := counter.stats.Active == 0 && counter.stats.Pending == 0
		if counter.changed == nil {
			counter.changed = make(chan struct{})
		}
		changed := counter.changed
		counter.mu.Unlock()
		if err := ctx.Err(); err != nil {
			return err
		}
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
