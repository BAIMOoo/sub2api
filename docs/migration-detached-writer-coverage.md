# Migration writer coverage: quiet handover

Reviewed against the migration worktree on 2026-10-16. This is source-level
coverage for the patched process. It does not certify an old/unpatched process,
an external writer, a plugin process, or later changes to these launch sites.

## Proof required before pausing data access

1. Reversibly pause HTTP admission and prove admitted requests = 0 and hijacked
   WebSockets = 0. Existing streams finish naturally.
2. Acquire every required writer fence. Missing wiring, a failed fence, a panic,
   a nonzero accepted-work counter, or a known failed buffered batch rejects the
   handover. A timed-out acquisition resumes acquired fences in reverse order.
3. Prove the lease is still held and each active/pending probe is zero immediately before
   pausing PostgreSQL/Redis. The data-access barrier remains the last defense.
4. Abort and reopen admission on the current authority if the bounded quiet
   window cannot complete. Never cancel business tasks to obtain zero counters.

The task registry is deliberately distinct from `safe_to_stop`: the latter does
not become true solely because this process is quiet.

## Required component boundaries

| Binding | Fence and evidence |
| --- | --- |
| `timing_wheel` | Pause callback admission; join actual executing callbacks; retain future callbacks. |
| `scheduled_writers` | Verify disabled startup configuration **and** actual startup history. All suppressed start closures are retained. |
| `plugins` | Parent-owned plugin gate; fail closed when an active plugin lacks an RPC/data-access fence. |
| `prompt_audit` | Parent-owned enqueue/claim/reclaim gates and accepted-task drain. |
| `prompt_config` | Config refresh/subscriber gate; Redis handover separately rebuilds real subscriptions. |
| `auth_outbox` | Fence `processBatch` before SQL claim; its child workers are joined. Durable outbox rows remain in SQL. |
| `ops_runtime` | Fence every periodic settings read before database access. |
| `usage_records` | Count before pool submission through execution; include inline/sample overflow; reject drops finish their tickets. |
| `billing_cache` | Count before enqueue through worker completion, including the reset goroutine. |
| `content_moderation` | Drain accepted async decisions while workers still run, then pause worker iterations/config readers and cleanup. |
| `detached_writers` | Count in the submitting goroutine before launch/enqueue and through all data access. Include child tasks before parent completion. |
| `deferred_last_used` | Serialize explicit flush and merge failed snapshots without overwriting newer timestamps. |
| `audit_log` | Flush queue and local batch; hold worker; failed accepted batch remains a sticky blocker. |
| `ops_ingress_reject` | Fence new aggregation; flush shards and retained failed batches; hold worker. |
| `ops_system_log` | Flush queue and local batch; hold worker; failed accepted batch remains a sticky blocker. |

Audit/system-log observations accepted after their producer cut point are kept
in a separate bounded hold and reported as `Deferred`. They cannot enter a
database consumer until resume, so they do not invalidate the existing quiet
lease or prevent control-plane recovery. Resume replays them through the normal
queue; ordinary overload/drop behavior remains observable.

## Detached task source inventory

`BeginMigrationDetachedTask` is called before launching request-derived work in:

- Account usage, model observation and balance/account-quota notifications.
- Shared gateway billing, OpenAI usage and in-flight billing cleanup.
- Admin/user/group/account/proxy/redeem/promo/subscription/payment actions.
- Dashboard fills, backup jobs and restores, idempotency completion, operation
  lock heartbeats and delayed upstream-billing lock release timers.
- HTTP-triggered usage cleanup, dashboard backfill and range recomputation,
  independently of whether each service's periodic startup was suppressed.
- Upstream cookie-jar persistence, OAuth callbacks, async media tasks and Grok
  import probes.
- OpenAI Live observers and its read/write bridge tasks; these may outlive HTTP
  and retain accounting until the natural Live session end.
- Concurrency lease refresh, WebSocket prewarm, and WebSocket session-preemption
  owner watchers/notification tasks.
- WebSocket V2 client readers, including later-turn `BeforeRequest`,
  `BeforeTurn` and model-mapping/data callbacks which can outlive `Relay` and
  the HTTP handler. `RelayOptions.BeginClientReader` registers the lifetime
  synchronously before launch; its completion is deferred by the child until
  every policy callback and all remaining reader work return.
- Admin QPS WebSocket cache refresh lifetime, including its natural idle-stop
  delay. An empty list of connected clients does not substitute for this count.

Accepted queued jobs are counted through dequeue **and** batch/worker completion
in the email queue, subscription-maintenance queue, handler ops-error queue,
repository usage-log Create/BestEffort batchers, content-moderation queue and
admin Grok-import probe queue. Rejected admission rolls back the ticket.

The seven `singleflight.Group.DoChan` sites use
`MigrationSingleflightDoChan`: Grok quota, billing quota, gateway settings,
channel-monitor quota, CN quota, CN balance and Codex models. The wrapper waits
for the actual shared callback even when a follower cancels or ignores its
buffered result. Ordinary mode retains the original singleflight call.

## Launch sites covered by lifetime or producer proof

- All service periodic worker `Start` calls are suppressed by
  `startMigrationBackgroundWorker` for a migration replica, including pricing,
  batch-image workers, snapshots/outbox, token refresh, quota reset, expiry,
  retention, probes, backups, aggregation and reports. DeferredService's timer
  is instead covered by the timing-wheel fence. Auth outbox, Ops runtime and
  moderation are explicitly gated because they remain running.
- Token-refresh workers, scheduled-test children, Grok-quota fanout, pinned
  Codex-model fetches, security-audit coordinator checks and HTTP errgroups join
  their children before the enclosing task returns. Batch-image submit and job
  heartbeats explicitly join `hbDone` before returning. Channel-monitor pond
  work is produced only by its suppressed scheduler.
- HTTP/SSE scanner, line-pump, keepalive, body-transform and raw socket helpers
  only move bytes or memory; data-changing result processing remains on the
  enclosing tracked request. WebSocket V2's policy-enforcing client reader is
  explicitly tracked separately: it does execute data callbacks and its
  existing second-exit wait is bounded to 200 ms. WebSocket V2's upstream reader
  also runs data callbacks, but `Relay` explicitly joins `upstreamDone` before
  settlement and return. OpenAI Live's separate observer is explicitly tracked.
- WebSocket pool idle readers/ping/cleanup manipulate upstream sockets and pool
  memory. Prewarm, which may read SQL/config or create nested prewarm, is
  separately tracked. Pool eviction does not start data work.
- Redis Pub/Sub sockets/retry supervisors are owned by the Redis migration
  barrier and real subscribe-ACK rebuild, not detached application tickets.
- OAuth package cleanup loops only expire in-memory state. Pool autoscaling,
  shutdown WaitGroup waiters, control HTTP serve, and stream timeout timers have
  no independent SQL/Redis/file access. Setup handler is outside normal
  initialized production server routing. Restart helper has no business writer.

The inventory was searched with `rg` for raw `go` statements plus `DoChan`,
`Submit`/`TrySubmit`, errgroup `Go`, and `time.AfterFunc`, across the service,
handler, repository, security-audit and remaining internal packages. Changes to
any of these launch/queue sites require reevaluating the boundary above.

## Restoring normal scheduled work

After PostgreSQL and Redis point to the final authority, all writer leases and
the timing-wheel pause are released, and the old application owner is confirmed
stopped, the private controller may call `ActivateMigrationBackgroundWorkers`.
It never restarts the HTTP process or disconnects streams. Activation rejects an
in-progress/held writer quiet lease; start closures run in stable order and are
attempted at most once. A partial-start panic is a sticky failure, not a retry.

`MigrationBackgroundActivationStatus` reports activation, in-progress state,
started/pending names, and failures. Started means the start closure was called;
services disabled by business configuration can correctly return immediately.
Any activation permanently prevents another quiet handover in that process,
even if an environment variable is changed. A later migration requires a fresh
replica with producers suppressed from construction.

## Dynamic evidence and limits

Race tests exercise parent-side admission, nested work, canceled/ignored
singleflight followers, full-queue rollback, dequeued work, moderation drain
ordering, auth-outbox children, Ops-runtime pause/resume, timer joins, deferred
merge/flush, consumer overflow, buffered local batches, failed batch evidence,
exclusive leases, panic rollback and once-only background activation. The WS
regression runs the real passthrough adapter with a later-turn `BeforeTurn`
callback held after the HTTP handler and WebSocket have closed; writer quiet
times out until that callback completes naturally. Core relay tests separately
prove synchronous registration before launch and completion on reader exits.

These tests prove boundary behavior; production readiness additionally requires
actual loopback status, SQL/Redis epochs, source-writer exclusion, copied-file
integrity, stream continuity and the controller's ≤10-second rollback window.
Long Live sessions, delayed releases and QPS idle-stop tasks can legitimately
keep detached counts nonzero. Their natural completion is required; the attempt
must otherwise abort and retry with the current authority still serving.
