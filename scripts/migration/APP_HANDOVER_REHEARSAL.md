# Actual application and replicated data handover rehearsal

Status: complete isolated run `3988afe9342082db` passed on Tencent on 2026-10-16, including actual PG/Redis handover and application restart with both old endpoints stopped and refusing TCP connections. Production streaming, billing and live-load acceptance remain separate. The staged `0bdcaa371aa4` compatibility build includes startup endpoint selection; the older `976be8f157f7` build cannot satisfy restart acceptance.

This is an isolated test on Tencent `43.165.177.136`. It uses a clone of the stopped cold-restore rehearsal volume, the actual compatibility application image, PostgreSQL **18.3**, and Redis **8.6.2**. It creates its own real physical PostgreSQL replica using `pg_basebackup` and performs a real Redis coordinated `FAILOVER`. Its synthetic SQL/Redis writes never enter production storage.

## Commands

From the shared migration worktree:

```sh
python3 scripts/migration/rehearse_app_handover.py
python3 -m unittest discover -s scripts/migration -p rehearse_app_handover_test.py -v
python3 -m py_compile scripts/migration/rehearse_app_handover.py scripts/migration/rehearse_app_handover_test.py
```

The first command is entirely offline. It does not invoke SSH, Docker, or a remote read.

The current reviewed staged build is:

```sh
python3 scripts/migration/rehearse_app_handover.py --apply \
  --app-image 'sub2api:migration-compat-20261016-0bdcaa371aa4' \
  --app-image-id 'sha256:73945bb5447cceaa5a301e0ddbf6fd026ef7cd27a9ad0596a542de691e29ce6a' \
  --binary-sha256 '0bdcaa371aa45abfa1eb61c763d2da10f133d718c4ad93251587ca0cc6191724'
```

The script verifies image identity, platform, production-baseline label and binary label, then independently hashes `/app/sub2api` inside the running isolated container. PostgreSQL/Redis image Config digests are pinned; Docker 29's manifest-versus-Config distinction is handled explicitly. SSH uses the independent Mac proxy route and strict host-key checking. There is no source AWS SSH command.

## Isolation and resource boundaries

Every invocation generates a fresh 16-hex run ID. Names are `sub2api-app-rehearsal-<id>-<role>` and owned volumes append `-data`. Containers and volumes receive the exact run ID in `io.sub2api.migration.app-rehearsal`; all later `exec`, `start`, and `stop` actions use captured immutable container IDs. A root-owned private file lock prevents two rehearsal instances from allocating concurrently.

An owned 16 MiB anchor has `--network=none` and runs only `/bin/sleep` from the same pinned PostgreSQL image. Its root filesystem is read-only and the image's data-volume path is masked with a 1 MiB tmpfs; it creates no data volume or PostgreSQL process. The application, both PostgreSQL instances and both Redis instances share that anchor's immutable container ID as their network namespace. The anchor is verified owned and running and remains alive until every data/application container has stopped. Only loopback exists in that namespace. No published ports, host network, Docker socket mount, business route or network rule is created. The application therefore has no upstream model access or access to any production database, proxy, Redis, or migration tunnel. Copied proxy variables and preexisting `MIGRATION_*` settings are also replaced or removed.

| Role | Isolated loopback port | Memory cap |
| --- | ---: | ---: |
| PostgreSQL source clone | 35432 | 256 MiB |
| PostgreSQL physical replica | 35433 | 256 MiB |
| Redis source | 36379 | 64 MiB |
| Redis replica | 36380 | 64 MiB |
| Application API | 38082 | 368 MiB |
| Private application controls | 38092 | Shares application cap |
| Network namespace anchor | None | 16 MiB |

The aggregate running cap is **1 GiB**, with swap disallowed. The 64 MiB copy helper completes before data/application containers start. Preflight requires at least another **256 MiB** of available host memory and `3 × cold input size + 3 GiB` free disk. Each container has CPU and PID caps and `restart=no`. Insufficient resources cause failure before creating volumes. Any OOM state causes the final result to fail.

The protected input is the stopped container `sub2api-migration-20261016-pg-rehearsal-130132` and volume `sub2api-migration-20261016-pg-rehearsal-130132-data`. That volume is mounted **read-only** in the copy helper and is never attached to a running PostgreSQL instance by this script. The script rejects tablespaces, symlinks, running input, unexpected PGDATA layout or version. It compares file identity/size/mtime fingerprints plus hashes of configuration/control files before and after, including on failure when preflight obtained the input fingerprint.

Only the newly owned PGDATA clone receives the local trust HBA. The replica comes from an actual streaming base backup of that clone, not another independent cold copy. Their physical PostgreSQL system identifiers must match.

The source runtime snapshot is read privately from `app-runtime/source-runtime.json`. Exactly `config.yaml`, `.installed`, `model_pricing.json`, and `model_pricing.sha256` are copied from `app-initial`, with verification against the staged manifest. Application credentials and private control token go into a root-readable **0600** `app.env`; neither enters shell arguments or logs. Each HTTP call rechecks the running app's owner label, obtains its immutable container's current PID, and uses `nsenter --target <pid> --net -- /usr/bin/python3` to open a real socket within that owned isolated namespace. The complete request, including Bearer header, travels only on stdin. The socket stays open until its response arrives; BusyBox `nc` closed its write half at stdin EOF and caused net/http to cancel queued requests, so it is no longer used. The token is never an argument to Python, nsenter, SSH, Docker, or a shell.

The application runs with quiet handover mode, `MIGRATION_BACKGROUND_WORKERS=false`, `MIGRATION_SHARED_REDIS=true`, PostgreSQL and Redis controls enabled, loopback endpoints, and explicitly enabled auth/subscription L1 caches. No deferred background activation is performed.

## Durable endpoint checkpoint

Each run has root-owned `endpoint-state/` mode **0755**. Its only authority document is non-secret `endpoints.json`, mode **0644**:

```json
{"v":1,"postgres":"source","redis":"source"}
```

The **directory** is bind-mounted read-only at `/migration-state`; `MIGRATION_ENDPOINT_STATE_FILE=/migration-state/endpoints.json`. Mounting only the file is prohibited by the design because it would pin an obsolete inode after replacement. A checkpoint update validates its prior content, writes a fresh file, fsyncs it, atomically replaces the filename, and fsyncs the containing directory. Only `v`, `postgres`, and `redis` labels are stored; addresses and secrets stay in the private startup environment.

After the new PostgreSQL primary is established, the PostgreSQL label is durably changed to `target` **before** the running application's SQL switch. After Redis coordinated role transfer, the Redis label is durably changed to `target` **before** its runtime switch. A crash or control rejection between checkpoint and switch therefore restarts against the established new authority. The deliberate rejected pre-promotion switch test does not update the checkpoint. Same-authority abort leaves both labels `source`.

## Acceptance sequence

1. Start the cloned PostgreSQL source, create a dedicated `migration_rehearsal.writes` table, commit a marker, and obtain a real streaming physical replica. Start both Redis instances, confirm Redis replication, and commit a Redis marker with `SET` and `WAIT 1 2000` on the **same connection**.
2. Start the actual compatibility application in private quiet mode. Require readiness from the real app, complete 15-component writer coverage, zero failed/dropped accepted-work counters, and exactly these six acknowledged Redis consumers: `auth`, `subscription`, `channel`, `error_passthrough`, `tls_fingerprint`, `prompt_audit`. Check that unauthenticated controls return 401. The business listener may return 404 or the baseline SPA's HTML fallback for an unknown private path; it must not return private JSON or execute a transition. POST the ingress-pause path on that business listener and independently prove the private ingress state stayed unchanged.
3. Measure three isolated `/health` requests as a baseline. Reject wrong epochs for ingress, writers, PostgreSQL and Redis; reject writer/Redis/PG transitions when their required quiet state is absent.
4. Exercise **same-epoch abort**: pause ingress; send a queued real HTTP health request; observe it in `queued_requests`; pause and drain writers; reject PostgreSQL-before-Redis and Redis-switch-before-pause; pause Redis; pause/resume PostgreSQL on its current source; reject resume before rebuild, wrong rebuild epoch, writer resume with paused Redis, and ingress resume with held writer fence. Rebuild all six consumers/caches on the **unchanged Redis epoch**, resume Redis, release writers, and reopen ingress. Require the queued health request to complete with 200 and the queue to return to zero.
5. Commit another real SQL marker **after** app initialization and physical replication. Pause admission and queue another health request. Drain all writers, then pause Redis **before PostgreSQL**. Require zero active SQL operations and reject switching to the unpromoted target. Wait for target replay through the exact source flush LSN; check warnings before any role change. Abort if too little of the 10-second budget remains to attempt role change.
6. Record the fixed owned source-container fence intent, request `pg_ctl stop`, and inspect that exact ID again. Require the same owner, restart=no, Running=false, status=exited, ExitCode=0 and no OOM before accepting the fence. The pg_ctl exec session may itself return nonzero or time out when the container's PostgreSQL PID1 exits; that session result is retained as evidence and never substitutes for actual container state. Promote PG2, durably checkpoint SQL target selection, switch the app's existing SQL client, then resume SQL while HTTP/writers/Redis remain paused. Execute real `FAILOVER TO 127.0.0.1 36380 TIMEOUT 1500` on Redis1. Require Redis2 primary, Redis1 connected replica, and retention of the confirmed Redis marker. Persist Redis target selection, switch the existing app client, rebuild all six subscriptions and local caches at its new epoch, and resume Redis. Confirm both SQL markers exist on the promoted database; check warnings before reopening admission. Release writer fence and admission; require queued health success within the measured budget.
7. Commit a third SQL marker on the new primary and a new Redis marker; verify the old Redis follows the new primary. This is synthetic persistence verification. It does not prove real model accounting correctness.
8. Stop Redis1, with PG1 already stopped. From the owned application namespace, require actual TCP `ConnectionRefusedError` from both old source ports; timeout alone is insufficient. Verify the independently owned namespace anchor remains running, then stop/start **only this run's app**. Require startup readiness, SQL `endpoint=target`, Redis address `127.0.0.1:36380`, all six acknowledged consumers, retained SQL writes, and successful health. Verify both source containers remain stopped and repeat both source TCP refusal probes. Record the selected endpoints, Redis epoch, all six consumer names/generations, anchor ID and TCP evidence. This proves fresh-process target selection with neither old endpoint available. Restart timing is reported separately from the cutover pause budget.
9. Verify the cold input remains unchanged. Stop all owned containers, retain every volume and report, and fail the final result if any owned container cannot be stopped or hit its memory cap.

All tested wrong epochs and invalid-order calls require HTTP **409**. Their acceptance is a failed rehearsal. The production `safe_to_stop` field is deliberately not used: it remains false by design and cannot replace the explicit admission, HTTP/WebSocket, writer, Redis, and SQL proofs.

## Warnings, failures, timing and evidence

The script checks writer failure/drop counters and scans the owned container logs privately for `warn`, `warning`, `error`, `fatal`, and `panic`. It returns counts only. Log text, user data and secrets are never printed or copied into public reports. Expected PG shutdown classification is enabled only after the fixed owned source container is verified to have exited cleanly, rather than when a role change is merely intended. Exact administrative connection/WAL-worker termination messages are then accepted only for PG1/PG2. PG1's exact `the database system is shutting down` FATAL and PG2's exact end-of-stream/receiver disconnect from isolated `127.0.0.1:35432` are also accepted. Another role, source port, severity or message remains a failure. Exact PG startup-readiness FATALs from the runner's own probes and two known Redis initialization lines are separately classified as expected isolated-test output.

Six exact WARN message strings were read back from the original production baseline on 2026-10-16 and are accepted only in the app's structured WARN field. They do not allow ERROR/FATAL, modified messages or another container role:

- `TOTP encryption key auto-generated. Consider setting a fixed key for production.`
- `security.url_allowlist.enabled=false; allowlist/SSRF checks disabled (minimal format validation only).`
- `[Pricing] Fallback merge skipped: open ./resources/model-pricing/model_prices_and_context_window.json: no such file or directory`
- `payment encryption/signing key is not explicitly configured; set TOTP_ENCRYPTION_KEY to enable payment resume tokens`
- `Warning: server.trusted_proxies is not configured; disabling the forwarded-IP compatibility switch will use direct peer addresses only`
- `Warning: CORS allowed_origins not configured; cross-origin requests will be rejected.`

The baseline configuration is preserved. No general startup-warning allowlist exists, and any new warning/error still fails the run. The owner separately verified that no existing users have enabled TOTP/encrypted TOTP secrets; this rehearsal does not alter those settings.

On any failure the app remains isolated and the runner stops its owned containers; it does not guess an authority and resume on the old endpoint. Existing production standbys, the shared migration channel, AWS, Caddy, public listeners, DNS, and unrelated services have no mutation path in this script. No volume deletion or cleanup command exists. Failed resources remain for root review and are not silently reused by a future run. A new invocation receives new owned names. A lost initiating SSH connection does not deliberately terminate the remote worker; SIGHUP is ignored so its final stop/evidence path can complete. A hard-killed host or runner may leave bounded owned containers for manual inspection; this script is not the production cutover watchdog.

The protected target report directory is:

`/home/ubuntu/sub2api-migration-20261016/app-handover-<id>/`

It contains `ownership.json`, `report.json`, private `app.env`, and non-secret `endpoint-state/endpoints.json`. The Mac receives only sanitized `request.json` and `summary.json` under:

`~/.codex/backups/sub2api-app-handover-<id>/`

`report.json` records every step duration, each scenario's total admission pause, health baseline, queued health completion, additional wait, actual marker checks, checkpoint selection, restart evidence, cold-input fingerprints, warning counts, OOM/stop failures and immutable resource IDs. A scenario passes the 10-second timing check only if **both** its complete admission pause and queued health delay above the measured baseline are at most 10 seconds. An overrun is explicitly a failure, even when eventual health succeeds. The pre-role-change time gate and role-change timeouts are safety checks; only actual measured execution establishes the observed duration.

**An isolated queued health request does not prove real model first-token latency, active SSE/WebSocket continuity, accounting durability for a billed model request, or recovery performance under live load.** The report always keeps `real_model_first_token_latency_proven=false` and `business_billing_semantics_proven=false`. Those live-path properties and the production watchdog require separate acceptance before any production handover. Offline protocol-model tests demonstrate script ordering and refusal behavior; they do not replace the real application/replication rehearsal.

## Current verification

- Offline default invocation succeeds without process or network tools.
- 33 offline tests cover isolation, owned identifiers, read-only cold input, nsenter socket/secret transport, private file modes, six consumers, writer loss counters, WebSocket quiet rejection, bad HTTP frames, same-epoch reconstruction ordering, real-handover orchestration ordering, 10-second overrun refusal, atomic endpoint replacement, retained checkpoints after rejection, restarting with both old endpoints stopped, source TCP-refusal evidence, independent anchor ownership/stop order, unchanged 1 GiB aggregate running cap, exact source-baseline/PG shutdown log exceptions, and clean source-container fencing despite a lost pg_ctl exec result.
- Python source and generated remote worker compile successfully.
- Preliminary run `78e40b9418063297` passed same-authority abort/rebuild/resume, with 1.833s total admission pause and 1.745s additional isolated health wait. Its second window passed all quiet controls/LSN checks and cleanly shut down PG1; the runner incorrectly treated the pg_ctl exec result as a failed fence. This script now uses the verified container-state rule described above. That partial run does not count as completed handover acceptance.
- After complete report/inspect/log archives and explicit owner review, exactly three retired failed runs (`3b99a859dcb16249`, `241177820829f50f`, `063f5126f90d169f`) were removed using immutable stopped container IDs and their fifteen named owned volumes. Every volume deletion checked owner labels and absence of container references; no force, volume cascade or prune was used. Protected cold input, ownerless anonymous volumes, target reports, other runs and production standbys remain. Local private archive manifest: `~/.codex/backups/sub2api-retired-rehearsal-archives-20261016.json`; cleanup evidence: `~/.codex/backups/sub2api-owned-rehearsal-cleanup-20261016-result.json`. Available disk rose from 7.06GB to 18.88GB with the original preflight threshold unchanged.
- Run `c40fee35def4be61` passed actual PG/Redis handover with 3.567s total admission pause and 3.480s additional isolated health wait. It retained all three SQL markers and both Redis markers and checkpointed both labels to target. Restart acceptance exposed Docker's refusal to rejoin the stopped PG1 container's namespace. The independent anchor described above removes that harness dependency while keeping both old data authorities stopped.
- Complete run `3988afe9342082db` passed all stages. Same-authority abort/rebuild/resume took **2.163s** of admission pause and added **2.068s** to the queued health request. Actual PG/Redis handover took **3.753s** of admission pause and added **3.662s** to queued health; both passed the unchanged 10-second gate. Both durable endpoint labels are `target`.
- Fresh-process restart took **1.668s**, measured separately from cutover admission. PG1 and Redis1 stayed stopped; both source TCP ports returned connection-refused before and after restart. SQL selected `target`; Redis selected `127.0.0.1:36380`, epoch 1. Consumers `auth`, `channel`, `error_passthrough`, `prompt_audit`, `subscription`, `tls_fingerprint` all acknowledged generation 1. Three committed SQL markers and confirmed Redis markers were retained, all seven owned containers stopped, warning counts and OOM/stop failures were zero, and cold-input fingerprints matched. All five owned data volumes remain retained.
- Target evidence: `/home/ubuntu/sub2api-migration-20261016/app-handover-3988afe9342082db/report.json`. Local private request, summary and complete report: `~/.codex/backups/sub2api-app-handover-3988afe9342082db/`. This result proves isolated actual-app/data/control behavior; the explicit model-stream and billing proof flags remain false.

The repository currently ignores `scripts/`; root must intentionally include these artifacts when committing the migration work. This script does not stage or commit files.
