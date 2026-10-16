# Migration usage foundation — 2026-10-16

This is an isolated implementation foundation, not evidence that production
traffic can be switched. Durable capture is off by default and separate from
runtime initialization. Normal requests retain the existing billing path until
the explicit capture phase is enabled. These exercises use isolated fixtures;
they neither deploy nor switch production traffic.

The current migration design uses a global admission pause and full quiescence
before the data handover: all already-admitted HTTP requests, WebSockets, usage
tasks, and other accepted writers must complete. If the pause budget expires,
admission reopens against the existing authority. This foundation does not
replace that gate or justify cutting over with unsupported requests in flight.

## Implemented contract

- `NewMigrationUsageEvent` freezes the normalized monetary command, complete
  usage log without credential-bearing association objects, request pricing
  time, resolved pricing JSON, monetary breakdown, multipliers, and platform
  quota intent. Its stable ID derives from request ID and API key ID. The
  complete event fingerprint fits the existing `VARCHAR(64)` dedup schema.
- `OpenMigrationUsageJournal` takes an exclusive process lock. `Append` writes
  a private temporary file, fsyncs it, publishes an immutable event with an
  exclusive hard link, then fsyncs the directory. It returns success only after
  these steps. Disk failures poison the handle and require recovery.
- Events carry a checksum. Corrupt files and reused IDs with different effects
  fail closed. Pending events replay in original settlement order.
- `NewMigrationUsageSettlementRepository` atomically commits the existing dedup
  claim, balance/subscription, API key lifetime quota and windows, account quota,
  scheduler outbox, platform quota, and synchronous usage log. A conflicting
  preexisting usage row rolls back the complete transaction.
- Windows use the original settlement timestamp (`Usage.CreatedAt`), independently
  of the original request pricing time. Delayed events never rewind later
  windows. Lifetime money still settles once. Account fixed and rolling reset
  SQL retains the existing configured timezone/reset policy.
- `Replay` leaves an event pending on settlement error, including an unknown
  commit result. Subsequent replay resolves the outcome through the same
  transactionally committed fingerprint before repeating effects. It also
  requires a repeatable cache reconciler to succeed before unlink + directory
  fsync acknowledges the event. It never calls a provider or recalculates prices.
- `WithMigrationUsageCapture` redirects the shared `applyUsageBilling` and
  `writeUsageLogBestEffort` path to the journal. Both gateway RecordUsage
  implementations are exercised. Their legacy money/cache increments,
  deferred work, and independent usage inserts are bypassed in this context.
  `RecordUsage == nil` and `capture.CheckPersisted() == nil` are both necessary
  before releasing a client completion.
- Actual pricing is captured at the calculation that produced the selected
  `CostBreakdown`, rather than supplied by a handler or looked up again after
  forwarding. Its allowlisted snapshot freezes the effective card, interval,
  pricing time, token counts, long-context policy, tier/reasoning, channel time
  rules and calculated multipliers. It contains no API key, account, group, or
  credential-bearing association objects.
- The snapshot is bound to the exact selected cost object and checked against
  its final values. Rejected response-model candidates cannot replace the
  selected price. Free Fast's actual Standard re-evaluation and token-plus-search
  surcharge are explicitly recorded. Missing observation or an unexplained cost
  mutation fails capture. Legacy `PricingAt`, `BillingModel`, and `ResolvedPricing`
  capture-option fields are ignored; they are not pricing evidence.
- Direct catalog pricing retains its existing calculation-time clock; the
  snapshot labels that source `catalog_direct`. Unified resolver pricing records
  the actual request `PricingAt` used by that calculation. No preexisting pricing
  behavior is changed merely to populate a snapshot.

## Default-off HTTP integration

The following selected handlers enroll before concurrency waits, pings, or
forwarding: OpenAI Responses, Chat Completions, and Messages; Gateway Messages
and Chat Completions. Enrollment samples the runtime capture phase once per
request. An initialized runtime with capture still disabled uses legacy billing.
A malformed enabled setting or an enabled but absent/closed runtime fails before
forwarding.

The response writer holds the entire non-streaming success or the SSE terminal
sequence. The usage submit methods intercept the original context before the
existing background-context wrapper, then perform `Finish`, synchronous actual
`RecordUsage`, persisted-intent verification, and `ConfirmPersisted` in that
order. No old asynchronous submission follows. Client cancellation does not
discard the final capture context. Missing proof, returned accounting error, or
a panic fails closed; a panic must not fall through to legacy accounting.

Early non-success responses preserve their original status/body. If persistence
fails before headers are committed, the helper emits an explicit 503, preventing
Gin's implicit empty 200. If deltas already streamed, it emits a generic error
without a successful terminal frame or another upstream request. A persisted
intent remains recoverable even when later accounting or downstream delivery
fails.

Capture enrollment requires explicit resource provisioning; there are no guessed
production buffer limits:

- `MIGRATION_COMPLETION_MEMORY_BYTES`
- `MIGRATION_COMPLETION_MAX_FRAME_BYTES`
- `MIGRATION_COMPLETION_MAX_HELD_BYTES`
- `MIGRATION_COMPLETION_SPOOL_DIR` (absolute, accessible directory)

The accepted maximum frame must cover existing response sizes, including a
Responses `response.completed` frame containing the complete output. Capacity,
spool volume, and these limits must be validated before enabling capture.

These hooks establish neither every route nor every billing protocol. Explicit
Responses image intent and Gateway Gemini are rejected while capturing. Other
media, embeddings, search-only, WebSocket, cyber-session, and billable error or
partial-result paths still need separate coverage. The completion writer only
supports reviewed, uncompressed LF/CRLF JSON SSE and non-streaming responses.
It does not intercept WebSockets. No global readiness probe is registered by
these HTTP hooks.

## Redis connection foundation

`NewMigrationRedisClient` retains the fixed client object used by existing
repositories while routing new sockets by epoch. Whole command/pipeline leases,
handshake operations, and background minimum-idle dialing participate in drain.
A switch requires paused admission and zero active operations, fences connects
that raced the epoch, and closes tracked old pool/PubSub sockets. The tracked
socket preserves `syscall.Conn` checks so stale pooled sockets are discarded
even with client command retries disabled.

Isolated tests cover PubSub reconnect and resubscribe delivery. That alone does
not prove that every invalidation consumer rebuilt its local state after a
message gap. The connection foundation initially fails readiness until the
runtime's explicit local-cache rebuild/consumer handover has been integrated
and verified. Plain TCP is the verified transport; TLS/opaque sockets, dynamic
credential callbacks, client-side caching, and maintenance endpoint redirects
are rejected rather than silently accepted.

## Required integration and phase gates

1. Every success body, SSE terminal frame, WS turn completion, media result,
   search result, embedding result, and billable partial/error path needs a
   reviewed integration. Only the selected HTTP hooks above intercept transport
   output. Forward has already emitted completion in uncovered handlers when
   existing code calls asynchronous RecordUsage; wrapping a worker is too late.
2. Call RecordUsage synchronously while holding terminal output; do not execute
   the old asynchronous settlement for the same request afterward. A returned
   forwarding error cannot be converted into success merely because an event
   persisted. WS coverage is not established by this foundation.
3. Only actual observed token-pricing calculations may be captured. A fresh
   post-request lookup or fabricated capture options is insufficient. Config/
   price update isolation and uncovered provider/media pricing still require
   end-to-end verification.
4. Every old Redis-to-DB platform quota flusher must drain/stop before platform
   DB authority starts. Disabling a worker only in the new instance is insufficient.
   Otherwise an old flusher can overwrite committed DB quota. The capture API
   rejects platform effects unless `PlatformQuotaDBAuthoritative` is explicitly
   established. This prerequisite is not satisfied on the live source yet.
5. Reconciliation must invalidate/rebuild balance, subscriptions, API key
   windows/auth views, platform quota, and account scheduling caches from DB.
   Redis increments are not repeatable after unknown commit. Replay must run
   before accepting new work after crash. Keep configuration frozen until
   pending events are resolved. The caller owns worker lifecycle/metrics.
6. Simple mode's usage-only path fails `CheckPersisted`; it is deliberately not
   silently accepted. Simple key-rate-limit mode uses the shared command path.
   OpenAI 403-counter reset before settlement remains a non-monetary Redis side
   effect in existing RecordUsage; isolated exercises must use isolated Redis.
7. Low-balance/account notification delivery and last-used deferred metadata
   are not made exactly-once by this foundation. A caller must provide a durable
   repeatable policy for any mandatory notification side effect. No journal
   readiness probe may report ready solely from directory/worker availability.
8. Use a provisioned durable local persistent volume supporting file fsync,
   directory fsync, hard links, and flock. These tests cover process death,
   not physical disk loss or a cloud-volume power-loss guarantee. Source
   events must remain recoverable throughout server retirement/rollback.

## Fresh verification

Executed against the isolated worktree, without production endpoints:

```text
go test -race ./internal/service \
  -run '^(TestMigration(Pricing|Usage)|TestOpenAIGatewayServiceRecordUsage|TestCalculateTokenCostForRequest)' \
  -count=1
PASS (4.018s)

go test -tags unit ./internal/repository -run '^TestMigrationUsage' -count=1
PASS (1.051s)

SUB2API_TEST_POSTGRES_IMAGE=postgres:18-alpine go test -tags integration \
  ./internal/repository -run '^TestMigrationUsageIntegration' -count=1 -v
PASS (4.673s; disposable PostgreSQL 18 and Redis containers)

go test -race ./internal/repository -run '^TestMigrationRedis' -count=10
PASS (3.057s; connection foundation before later runtime rebuild integration)

go test -race ./internal/handler \
  -run '^(TestMigration(Completion|HTTPUsage)|TestOpenAIResponses_APIKeyPassthrough|TestOpenAIRecoverResponsesPanic)' \
  -count=1
PASS (4.731s; includes the panic-fallback regression)
```

Cases include process exit after fsynced append and before worker execution;
lost commit response and duplicate replay; repeated cache reconciliation;
file/directory fsync failures; corruption, conflicting payload and double-open;
new persistence while replay waits on DB; gateway and OpenAI capture with no
legacy money/log calls; unhanded quota authority and unsupported simple path;
real PostgreSQL atomic settlement/duplicate/outbox; fixed/rolling window
timestamps and no rewind; final usage insert error rolling money and dedup back.

Pricing cases include channel interval/time/reasoning/priority rules; cheap
response-model adoption and expensive-candidate rejection; selected fallback;
Free Fast's selected priority card and actual Standard customer charge; historical
DeepSeek peak and pro-to-flash boundary; search surcharge; and missing/mutated
pricing failing closed.

HTTP cases hold Responses, Chat, Messages, and non-streaming completion until a
real service `RecordUsage` append is durable. Six concrete OpenAI handler cases
(three protocols, streaming and non-streaming) run actual Forward adapters against
an isolated in-memory upstream and produce a positive durable billing intent.
The legacy balance method panics if reached. These cases inject capture through
the scoped handler helper rather than replacing the startup-only runtime; they
do not prove full startup/runtime activation. Gateway hooks compile and their
submission path is exercised; a concrete Anthropic Gateway upstream exercise
has not been added. No test calls a live production provider.

The panic regression covers both ordinary and mandatory submission for both
handler types. A task that panics after appending its durable intent executes
once, retains the pending event, and emits a failure without releasing success
or invoking the old settlement path.

Broad `-tags unit` service compilation encounters an existing duplicate
`mustJSON` helper in `openai_codex_body_encoding_test.go` and
`batch_image_cleanup_test.go`. Ordinary targeted service tests pass. This
preexisting compile gap must not be described as broad unit-suite success.

Production migration readiness remains **not established by this foundation**.
Capture may remain disabled under the stricter global-quiescence migration
protocol. Any future capture activation still requires verified transport and
pricing coverage, durable storage sizing, cache rebuild, source flusher handoff,
and runtime/lifecycle checks; a local journal or buffer test cannot establish
those conditions by itself.
