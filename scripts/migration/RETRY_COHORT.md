# Recovering attempt 1 without resetting its waiting history

The entry and application are on Tencent. Attempt 1 stopped before any source
PostgreSQL shutdown or Redis failover. Its recovered checkpoint permanently
retains `pause_seconds=7.082038551568985`, `attempt=1`, AWS/AWS authority and a
closed admission window. The source app exited cleanly. The target arm is
cancelled; the checkpoint's old arm response is historical evidence.

`prepare_retry.py` is offline by default. Its apply path upgrades only reviewed
Python control programs and proves a new request cohort. It never starts main,
pauses a request, promotes a database, changes DNS or reloads Caddy.

The target proof requires the same app process and instance, a completed
restore, AWS/AWS authority, source/source endpoints, cancelled arms, no promotion
intent and no unfinished RPC operation. Ingress reads bracket **one runtime
snapshot**, which holds queued, active and hijacked counters under the same Go
mutex. All three counters must be zero with the restore's open ingress epoch.
Queue admission transfers a waiter to the active counter under that mutex, so
there is no counter gap. This proves that previous requests ended or cancelled;
it does not prove that every request succeeded. Active streams and WebSockets
must finish naturally before authorization.

The source requires the exact recovered attempt, inactive main with an empty
cgroup, a healthy non-recovering guard, retired old monitors, the original
running data processes, an owned clean application stop, and no irreversible
operation intent. The installer archives the old checkpoint and drain proof,
creates a unique root 0600 record with O_EXCL and fsync, then CAS-replaces the
exact old checkpoint under the backend lock to add the record reference. An
unreferenced record cannot grant budget. Operation ledgers and attempts remain
unchanged. Every local stage has an exclusive intent and result or unknown
receipt; unknown outcomes require inspection.

`budget_accounting` verifies the immutable record's raw SHA, owner, run, previous
checkpoint digest and closed-source boundary. It derives the current cohort's
baseline from that previous checkpoint, never a caller-supplied numeric offset.
Global pause seconds continue to accumulate. Engine, independent guard, complete
verification and runtime adoption all enforce the same per-cohort interpretation
and retain the maximum historical cohort delay and sticky breach status.

The internal allowance remains 8 seconds per cohort and the user limit remains
10 seconds per request. A second pause within one cohort shares its remaining
allowance. Process restart and unknown record contents cannot grant a new one.

The updated combined path uses `pg.arm-quiet`: arm is durable before a complete
fresh quiet snapshot is sampled outside the PG flock. Each source invocation
creates a new in-memory nonce and requires an exact echo. Mutation transport
sends once; unknown outcomes recover. A same-attempt cached response cannot
satisfy a fresh invocation's nonce. Source-side sampling still includes the
whole RPC and must complete within 2 seconds. Recovery obtains its own new
evidence and never uses this snapshot to authorize a source stop.

The combined window contains 10 cross-cloud RPC calls, plus the runtime read
before opening it. The source-stop gate still reserves 5 seconds for forward
completion. Keep the full intent/effect/result durability boundaries, complete
ledgers and audit hashes; performance improvements cannot remove these gates.

## Admission-race recovery and waiting observations

An idle sample cannot exclude a request arriving immediately before
`ingress.pause`. If that happens, quiesce stops at the admission barrier; data
components remain open and the accepted SSE or WebSocket continues. Restore
may reopen admission with active requests only after verifying the same app
instance, serving/non-draining state, complete readiness, matching ingress
epoch, unheld writers, open data components and Redis cache readiness. Every
restore also checks the actual PostgreSQL endpoint and Redis address against
the confirmed authority and persistent endpoints **before** cancelling the old
quiesce or issuing a resume. Unknown authority and partial data pauses never
use the active-request exception. Cancellation is durable before ingress is
resumed, so recovery cannot replay the old pause.

Waiting uses `app.observe` with only a fixed GET `*.status` control. It validates
the normal request/owner/epoch and resource identity, samples afresh, and creates
no operation, audit entry or epoch change. The source observer also writes no
transport state and refuses epoch drift. The runtime's embedded ingress is the
same atomic request snapshot. Quiet attestations, mutations, authority changes
and recovery operations keep their durable path and full history.

`admission_upgrade.py` was installed once at attempt 1, AWS/AWS, closed-window,
unused-cohort boundary. It checked source first, then CAS-upgraded target and
source programs, preserved all ledgers and budget records, and ran three fresh
cross-cloud observations without changing source state. Do not rerun this or
`prepare_retry.py`: their frozen old pins and exclusive receipts deliberately
refuse replay. Neither installer starts the main controller.

The isolated HTTP regression retained 662 operations / 1,414,875 bytes and used
the real RPC HTTP client, response sanitation, loopback TCP and 56 file/directory
fsyncs. In the ingress race, complete invocation and restore took 0.585 seconds;
the queued HTTP request waited 0.354 seconds and returned 200 while the original
SSE continued. It used a Python admission/control model, not the Go production
runtime or the cross-cloud production path; it supplements the existing Go
gate tests and full isolated application handover rehearsal.
