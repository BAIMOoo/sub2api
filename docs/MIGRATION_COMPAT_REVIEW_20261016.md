# Quiet migration compatibility review — 2026-10-16

Independent source review of the compatibility worktree, without remote access
or production changes. The reviewed tree was being edited concurrently; this
report distinguishes findings from fixes reported by the leader. No test run
or successful production rehearsal is implied by this review.

## Release disposition

Original review disposition: do not deploy the reviewed snapshot until the
WebSocket client-reader lifetime below is covered and its regression passes.
The leader reported fixes for the first two findings while this review was
running. The remediation receipt at the end records later source changes and
local verification; isolated production rehearsal remains a separate gate.

## 1. Post-fence logging could make the control plane unrecoverable

At review time `migrationBufferedWriter.accept` incremented the main pending
counter unconditionally (`backend/internal/service/migration_buffered_writer.go:28`).
`OpsSystemLogSink.WriteLogEvent` used it at
`backend/internal/service/ops_system_log_sink.go:146`, while a successful flush
request parked its consumer until the quiet lease was released (around line
299). Audit logging used the same mechanism.

A warning accepted after successful writer quiescence, and after the data
barriers closed, made `MigrationWritersQuiet` fail. PostgreSQL resume required
that proof; Redis reconstruction required the same proof; releasing the writer
lease required ready endpoints. Thus a normal diagnostic could prevent the
controller from aborting a quiet attempt and reopening admission.

This has a concrete producer outside the business-handler fence:
`openai_ws_pool.go`'s `runBackgroundPingSweep` logs warnings for failed idle
upstream pings (around lines 986–1014), and those socket maintenance workers
continue running during quiet handover.

The leader reports adding a separate retained queue for events arriving after
the fence. Required regression: close PG/Redis barriers, emit such a warning,
abort on the existing authority, and prove admission can reopen within the
budget and the retained event is eventually handled.

## 2. Three HTTP-triggered SQL jobs lacked detached lifetime tickets

The original snapshot started these jobs after returning their HTTP-facing
method, without joining them or registering detached work before launch:

- `backend/internal/service/usage_cleanup_service.go:153`: `CreateTask` launched
  `runOnce`, which claims/deletes/updates SQL cleanup jobs.
- `backend/internal/service/dashboard_aggregation_service.go:144`:
  `TriggerBackfill` launched database backfill.
- `backend/internal/service/dashboard_aggregation_service.go:169`:
  `TriggerRecomputeRange` launched recomputation and could sleep between retries.

Disabling service startup suppresses periodic producers, but does not suppress
these methods. They could remain logically active after HTTP/WS counts reached
zero and after the writer lease was declared quiet. A SQL checkout barrier can
delay the next statement, but does not prove the accepted job finished.

The leader reports adding tickets before each launch. Required regression:
hold an accepted job after its HTTP request returns, and verify quiet cannot
commit until its final data access and any nested work finish.

## 3. WebSocket client reader can outlive HTTP while executing data callbacks

**Unresolved in the original reviewed snapshot; remediation is recorded below.**

`backend/internal/service/openai_ws_v2/passthrough_relay.go:285–289` starts
`runClientToUpstream` without a detached lifetime boundary. On an upstream/error
exit, the relay waits for another exit signal for only 200 ms (lines 355–359).
It then joins only `upstreamDone` (line 378), so the client reader is allowed to
outlive `Relay` and its enclosing HTTP handler.

That reader does more than move bytes. Its `ReadClientFrame` calls a policy
wrapper. The filter in
`backend/internal/service/openai_ws_v2_passthrough_adapter.go:1052–1064` invokes
`hooks.BeforeRequest`, `hooks.BeforeTurn`, and `hooks.MapRequestModel` for later
`response.create` frames. The handler's `BeforeTurn` callback in
`backend/internal/handler/openai_gateway_handler.go:2948` performs Redis-backed
concurrency admission and billing checks. Other policy hooks can read SQL or
enqueue side effects. These callbacks use the outer handler context; they are
not guaranteed to finish when `relayCtx` is canceled.

Reproduction shape:

1. Establish a WS passthrough session and send a later `response.create`.
2. Hold its policy/`BeforeTurn` callback for longer than 200 ms.
3. End the upstream relay while that callback is held.
4. Allow the WS and HTTP handler to close and request writer quiescence.
5. Observe that the client callback has no detached ticket and can remain alive
   after the request/hijacked counts become zero.

Minimal repair: expose a client-reader lifetime hook in the relay that acquires
the detached ticket synchronously **before** the `go` launch and completes it
after the reader, including policy callbacks, returns. Connect it in the
service adapter. A deliberate close-and-join implementation is another option,
but must preserve current failover and WebSocket close-frame behavior; joining
an unclosed client read can deadlock because its adapter uses the outer control
context. Merely registering a ticket inside the goroutine or for each callback
would leave an admission gap.

Required regression: hold the later-turn policy callback, make `Relay` return,
prove the detached count remains nonzero and quiet acquisition refuses or
times out, then release the callback and prove it reaches zero naturally. Also
verify the hook starts before the child is launched and finishes on every exit.
Update `docs/migration-detached-writer-coverage.md`, which currently classifies
WebSocket helpers as byte/memory-only and mentions joining only the upstream
reader.

## Other reviewed boundaries

- HTTP admission is checked on each handler invocation, so requests on reused
  keepalive connections remain gated. Hijacked connections are counted until
  their actual tracked connection closes; request drain alone is not claimed
  to prove safe process termination.
- Background activation and writer quiet acquisition share the registry lock.
  Activation marks the process permanently ineligible for a later quiet lease,
  and rejects an existing/acquiring writer lease. The private activation route
  additionally checks initialized controls, target-selected/resumed PG, target
  Redis, and endpoint readiness. Remote old-owner retirement remains the
  independent runner's responsibility.
- PostgreSQL private operations require the Redis consumers/command barrier to
  be paused first. PostgreSQL should resume before SQL-backed Redis cache
  reconstruction; every Redis pause requires reconstruction before Redis resume.
- Migration controls and destructive startup-cleanup suppression are opt-in.
  Several counters/timing/buffer implementations also change ordinary-mode
  internals; this review did not establish full behavioral equivalence for
  those changes. Final targeted tests and isolated application rehearsal remain
  required.

## Remediation receipt — 2026-10-16

The actual relay package is `backend/internal/service/openai_ws_v2`, not
`internal/pkg/openai_ws_v2`. The minimal lifetime repair is now implemented:

- `RelayOptions.BeginClientReader` runs synchronously in `startClientReader`
  before the `go` launch. The child defers the returned completion until
  `runClientToUpstream` returns, including later-turn policy/data callbacks.
- The passthrough service adapter supplies
  `BeginMigrationDetachedTask("openai_ws_v2.client_reader")`. Registration is
  never deferred into the new goroutine or limited to individual callbacks.
- The default hook is nil. The existing 200 ms second-exit wait, upstream join,
  transport close frames, graceful/error classification and failover paths are
  unchanged. No blocking client join or forced cancellation was introduced.
- `migration-detached-writer-coverage.md` corrects its earlier byte-only
  classification for the policy-enforcing client reader.

Dynamic regression `TestMigrationWSReaderBeforeTurnRemainsVisibleAfterHTTPReturn`
runs the real service adapter with an actual client WebSocket. It holds the
later-turn `BeforeTurn` callback, ends upstream, processes the existing normal
close handshake, and waits until the HTTP handler and client socket finish.
The detached site count remains 1; acquiring the writer quiet lease times out.
After releasing the callback, its final simulated data access completes, the
count reaches 0 naturally, and quiet acquisition succeeds. This passed under
the race detector 10 times.

Core relay lifetime regressions also passed under the race detector 10 times:
registration completes before child launch; a callback can remain accounted
after Relay returns; read EOF, policy error and canceled upstream-write paths
complete the ticket; a reader never started by the downstream gate acquires no
ticket. The entire `internal/service/openai_ws_v2` package passed with `-race`,
as did the existing `TestPassthroughIngress*` service cases. `go vet` on both
service packages and `git diff --check` passed.

The leader's fixes for findings 1 and 2 were rechecked at their source boundary.
Post-fence audit/system-log events now enter a bounded held queue reported as
`Deferred`, outside active/pending SQL-work probes, and replay on resume. The
three HTTP jobs acquire their detached tickets before launch and defer them
around the entire task/retry lifetime. Audit/system-log local-batch/resume tests
and `TestMigrationWriterQuietRetainsHandoverWarningsUntilResume` passed with
`-race` during this remediation verification. This receipt does not replace the
leader's integrated PG/Redis abort rehearsal or certify external/old writers.
