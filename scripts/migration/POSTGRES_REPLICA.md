# PostgreSQL 18.3 physical replica preparation

This tool is an isolated preparation step for the 2026-10-16 AWS→Tencent
migration. Its default invocation performs an offline dry run: no SSH or Docker
command runs and no state directory is created. As of this delivery, only local
mock-based tests, Python compilation, generated shell syntax, and the default
plan have been exercised. A leader-authorized read-only `--inspect` subsequently
passed against both hosts; no mutation has been executed by this helper's author.
Production preparation still needs the leader's review.

The tool cannot promote a database, change business data, restart the source
application/PostgreSQL, change DNS, or change application DSNs. The existing
target drill container is outside its fixed names and is never touched.

## Fixed scope

| Item | Value |
| --- | --- |
| Source SSH | `sub2api` via `prepare_channel.py`; reviewed IP `13.231.143.136` |
| Source container/database/admin | `sub2api-postgres` / `sub2api` / `sub2api` |
| Source server | exactly PostgreSQL 18.3; writable primary; no other slots/replicas |
| Source data/config | `/var/lib/postgresql/data` and its ordinary config/HBA files |
| Target SSH | `43.165.177.136`, existing restricted command path from `prepare_channel.py` |
| Locked image | `postgres:migration-20261016-ff4a089dadda`; Config digest and ordered RootFS DiffIDs must equal source |
| Target container | `sub2api-migration-pg-replica-20261016` |
| Target volume | `sub2api-migration-pg-replica-data-20261016` |
| Replication role | `sub2api_mig_20261016_replica` |
| Physical slot | `sub2api_mig_20261016` |
| Target listener | `127.0.0.1:25432`, host network; no Docker published ports |
| Replication source | `127.0.0.1:15432`, existing encrypted SSH channel |
| Source address as PostgreSQL sees it | `172.18.0.1/32` |
| Source private state | `/home/ubuntu/.local/share/sub2api-migration-20261016/postgres-replica` |
| Target private state | `/home/ubuntu/sub2api-migration-20261016/postgres-replica` |
| Local private state | `~/.local/share/sub2api-migration-20261016/postgres-replica` |

Directories are 0700; credentials, manifests, telemetry and private diagnostics
are 0600. Remote state is root-owned. The local controller uses an exclusive
lock. A random owner token, fixed run name, source system identifier/timeline/
container ID, and Docker owner labels protect against adopting another run.

## Commands for leader review

Run from the repository root:

```sh
# Entirely offline; prints the planned steps.
python3 scripts/migration/prepare_postgres_replica.py

# Read-only remote facts. It does not create remote state, roles, slots or containers.
python3 scripts/migration/prepare_postgres_replica.py --inspect

# After review, explicitly prepare and wait up to 10 minutes for the standby.
python3 scripts/migration/prepare_postgres_replica.py --apply

# Recheck a started preparation without creating more objects.
python3 scripts/migration/prepare_postgres_replica.py --status

# Offline cleanup plan; does not delete anything.
python3 scripts/migration/prepare_postgres_replica.py --cleanup

# Explicitly delete only this run's unpromoted replica and source objects.
python3 scripts/migration/prepare_postgres_replica.py --apply --cleanup
```

`--inspect` and `--status` cannot be combined with `--apply` or `--cleanup`.
`--state-dir` selects an absolute private local directory. Do not print or attach
its password, verifier, or private diagnostic files. After a completed cleanup,
archive all three private state directories under a new evidence name before
starting a fresh run; the tool deliberately refuses to reuse cleaned state.

## Facts that must be reviewed before mutation

The supplied facts are expectations, not a substitute for a fresh `--inspect`.
In particular, inspect must confirm:

- Source 18.3, expected superuser/admin, writable primary, stable system
  identifier/timeline and container ID, `wal_level=replica`, enough WAL senders
  and slots, no unrelated slots/replicas, SSL/archive mode off, and no config/HBA
  parse errors or pending restart for the relevant setting.
- Config and HBA reside in the expected persistent PGDATA directory. A direct
  file bind mount, symlinked WAL directory, external config, or custom tablespace
  requires a separately reviewed mapping and is refused.
- Source `pg_basebackup` and `pg_verifybackup` binaries are 18.3; source postgres
  UID/GID are recorded. Config digest plus ordered RootFS DiffIDs fix the target binaries and
  account identities. Source PGDATA and `pg_wal` sizes, WAL counters/LSN, current
  activity count, and free space are returned without query texts or credentials.
- Source free space is at least 3 GiB before preparation; the current expected
  6.8 GiB is not assumed. Target free space covers PGDATA plus 4 GiB for a new
  volume; the current expected 30 GiB is not assumed.
- Target has at least 1800 MiB total RAM and 900 MiB available before starting a
  new container; the standby is limited to 768 MiB memory/swap, one CPU, 128
  processes, and `shared_buffers=128MB`.
- The locked image exists and its Config digest and ordered uncompressed RootFS
  DiffIDs equal the source image; the source
  PG channel accepts a loopback TCP connection; port 25432 and reserved object
  names are free, or objects belong to this exact owner for a retry.
- The copied HBA permits local diagnostics for the existing `sub2api` admin.
  The tool does not weaken target local authentication to obtain access. If the
  existing Unix-socket authentication cannot run read-only psql diagnostics,
  readiness will remain false and private diagnostics must be reviewed.

A TCP channel check establishes reachability. SCRAM authentication by the new
role and the resulting replica's system identifier establish which PostgreSQL
cluster was copied. The source primary continues accepting its normal traffic.

The successful read-only inspection returned PostgreSQL 18.3, source system ID
`7626048206782038050`, no slots, source free 6.70 GiB, target free 27.32 GiB and
1445 MiB available RAM. Both images resolve to Config digest
`sha256:ff4a089dadda5e50a5f09604b2e2bc16f97d5befb6b05c328fc21159da96c602`
and the same nine ordered DiffIDs. Docker 28 classic reports a Config ID, while
Docker 29 containerd can report a manifest ID; these local image IDs are expected
to differ. The target reads and hashes the local `ctr -n moby content get`
manifest/config content instead of comparing those incompatible IDs. A target's
own local image ID is still used to detect changes to its created container.

## Preparation order and source effects

1. Store the local inspected identity; recheck source facts remotely; create a
   private owned source manifest before any source changes.
2. Install and verify an independent root systemd timer/service:
   `sub2api-pg-replica-20261016-source-monitor.{timer,service}`. The timer runs
   every 5 seconds; a fresh successful independent sample is required before
   proceeding. It is installed before any retained slot exists.
   A separate `...-source-monitor-fence.service` executes a short emergency path
   on monitor/timer failure, including main-monitor crashes/timeouts, and retries
   failed fencing every 5 seconds. The main service allows 30 seconds; the fence
   service allows 20 seconds with 1.5-second SQL and 0.5-second lock limits.
3. Record the old `max_slot_wal_keep_size` effective value and whether an
   assignment already exists in `postgresql.auto.conf`. If unlimited or greater
   than 512 MiB, use `ALTER SYSTEM SET ... = '512MB'` and `pg_reload_conf()`, then
   poll until effective. An existing stricter positive cap is preserved. No
   restart or forced `CHECKPOINT` occurs. A reload failure cannot create a role
   or slot; the restore intent remains on disk.
4. Create the dedicated non-superuser replication/login role with a 30-day
   credential lifetime and four-connection limit. CREATE and its ownership
   COMMENT commit in one transaction. The independent random password is
   generated locally; only a SCRAM-SHA-256 verifier reaches source SQL.
5. Prepend exactly this marked HBA identity and reload, preserving the file's
   owner/mode and all preexisting bytes/rule order:

   ```text
   host replication sub2api_mig_20261016_replica 172.18.0.1/32 scram-sha-256
   ```

6. Recheck that no other slot appeared, record slot-create intent, then create
   this named physical slot with immediate WAL reservation. There is no source
   data backup/archive file.
7. On target, install a second independent invariant/disk monitor, create the
   dedicated labelled volume/container, and start it detached. It copies the
   0600 replication password file into a private postgres-owned runtime directory.
   No password is passed in argv or emitted to stdout.
8. Inside that new target container, run `pg_basebackup` directly through the
   existing encrypted loopback channel with plain format, `--wal-method=stream`,
   `--write-recovery-conf`, the existing named slot, spread checkpoint, `8M`
   rate limit, SHA256 manifest and default fsync. WAL streaming is not throttled
   by the data-copy rate limit. `pg_verifybackup --exit-on-error` must succeed
   before a completed-seed marker is created and PostgreSQL starts. A nonempty
   partial directory is never automatically erased or reseeded.
9. PostgreSQL starts with loopback-only listening, hot standby and default
   transaction read-only on. Status requires recovery, read-only, expected
   identity/version/slot, a streaming WAL receiver, source async streaming,
   active valid slot, bounded WAL policy, disk margin, and healthy independent
   monitors on both sides. Three consecutive samples are required by apply.

The tool's `standby_ready` means *preparation verified*, allowing up to 64 MiB of
observed replay lag. It is not permission to cut over or an RPO allowance. The
later data handover must fence all old writers, prove the final exact LSN has
replayed, and follow the separately reviewed global-quiescence procedure.

## WAL and failure policy

PostgreSQL 18 checks `max_slot_wal_keep_size` at checkpoint time. The 512 MiB
setting is not an instantaneous disk quota. This tool also samples retained WAL,
`pg_wal` allocation, free disk, source streaming and lag independently of the
controller/SSH session.

At retained WAL ≥384 MiB, a lost/unreserved owned slot, an unbounded changed cap,
or source disk <2 GiB, the source monitor records a persistent safety trip,
sets **only the owned role** NOLOGIN, terminates only that role's WAL senders,
and drops only the owned physical slot after it is inactive. This deliberately
sacrifices the replica and requires explicit cleanup/reseed. It never terminates
business queries. Disabling the role prevents reconnect races while the slot is
removed. A later sample retries a partially completed safety action.

The source checks its recorded data-filesystem free space before the larger
metadata/statistics query. Any sampling exception records a persistent safety
trip and attempts a minimal container/cluster/timeline/role-owner verification
plus fence. This emergency path does not repeat the full `source_facts` query,
HBA parsing, source directory walk, or systemctl health sampling. An OnFailure
service independently retries it if the main process fails or times out.

The target monitor stops only its labelled container if disk drops below 2 GiB
or a queried identity/recovery/read-only/listener invariant fails. A temporary
receiver disconnect or lag makes readiness false but does not itself stop the
standby. Monitoring samples and a bounded telemetry log remain in each private
scope. Missing, failed, inactive, or >20-second-old monitor samples make the
controller's readiness false. Private raw stderr is captured without printing
SQL or DSNs.

If PostgreSQL/systemd/the host is unavailable, a monitor cannot promise a timely
action. Deliberately stopping all timer/service processes is also outside their
ability to protect themselves. The checkpoint cap plus monitor is a conservative guard, not a proof of
a physical disk hard limit under arbitrary WAL generation or host failure.
Inspect real WAL generation and source disk during the first supervised seed;
abort this preparation through explicit cleanup if the measured margin is
insufficient. No successful helper result establishes production failover
readiness.

A planned future promotion must transfer responsibility from the standby
invariant monitor under the cutover procedure; otherwise that monitor will
correctly stop a database that ceased to be a standby. Do not invoke cleanup to
perform that handover: cleanup deletes the replica.

## Interruptions, retry and cleanup

The controller never automatically rolls back after an exception or Ctrl-C.
A detached basebackup/standby and both monitor timers can continue safely while
the local controller is absent. State survives source GUC changes, role/slot
creation, target container creation, and unknown command outcomes. `--status`
reports the current owned objects; invoking `--apply` again adopts only matching
owned running/created objects and never starts a stopped partial seed. A failed
seed or safety trip requires explicit cleanup and a fresh state scope.

Cleanup first verifies ownership/unpromoted state, then stops/removes only the
new target container/volume. A completed seed without `standby.signal`, or a
queried primary, is refused even if its container disappeared. A nonempty
unverified volume without its exact seed container is also refused. It next fences/drops the dedicated source role and
slot, removes exactly the marked HBA block (preserving later unrelated edits),
and restores the old GUC only if the current effective/auto.conf values still
match this run and no other slot appeared. It will not overwrite another
operator's changed setting. Original source authority never moves.

Cleanup is idempotent after partial completion, including a lost response to a
GUC restore. It removes only exact owned monitor units after cleanup, erases the
replication password files, and retains private manifests/backups/telemetry for
review. It never deletes the existing target drill container or unrelated
volumes, roles, slots, HBA rules, systemd units or configuration lines.

## Local verification and limits

```sh
python3 -m py_compile scripts/migration/prepare_postgres_replica.py
python3 -m unittest discover -s scripts/migration -p 'prepare_postgres_replica_test.py' -v
python3 scripts/migration/prepare_postgres_replica.py
```

The 24 offline tests cover no-command defaults, mode separation, monitor/cap-before-
slot ordering, failed cap reload preventing role/slot creation, preserving a
stricter cap, rejecting source/version/slot drift, exact HBA cleanup with later
edits, private errors and SCRAM generation, container/backup command safety,
generated shell syntax, guard action, receiver reconnect behavior, stale monitor
health, missing-container cleanup refusal, sampling-timeout emergency fencing,
generated remote-program compilation, classic/containerd content equivalence,
minimal-fence identity/effect scoping, Docker missing-object errors,
incomplete cleanup and lost GUC-restore response. They use fake remote
runners; beyond the successful read-only inspection, they do not establish mutation SQL validity, source headroom under backup load,
Docker startup compatibility, independent systemd operation, live channel
identity, real backup verification, or operational readiness. Those remain
explicit reviewed execution checks.

## PostgreSQL 18 primary references

- [pg_basebackup](https://www.postgresql.org/docs/18/app-pgbasebackup.html):
  `-X stream`, existing named slot, `-R`, spread checkpoint, rate limit/fsync.
- [Replication settings](https://www.postgresql.org/docs/18/runtime-config-replication.html):
  `max_slot_wal_keep_size` applies at checkpoint and can invalidate a lagging slot.
- [Standby setup](https://www.postgresql.org/docs/18/warm-standby.html):
  `standby.signal`, `primary_conninfo`, `primary_slot_name`, asynchronous behavior.
- [HBA rules](https://www.postgresql.org/docs/18/auth-pg-hba-conf.html):
  physical `replication` identity, first matching rule, reload behavior.
- [pg_verifybackup](https://www.postgresql.org/docs/18/app-pgverifybackup.html):
  manifests/WAL are checked; generated `postgresql.auto.conf` and standby signal
  modifications are expected exceptions.
- [Password files](https://www.postgresql.org/docs/18/libpq-pgpass.html):
  `replication` password-file database matching and required 0600 Unix permissions.
