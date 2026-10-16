# AWS independent runner guard, run 20261016

This module is a local AWS guard, not an installer. Running it without exactly
`guard` prints an offline description and performs no local actions. No remote
apply or production verification is part of its offline test suite.

## Fixed interface for the installer

Install Python sources as root-owned `0644` files beneath the root-owned,
non-writable `/usr/local/libexec/sub2api-migration-20261016` directory. Install
the exact strings returned by `runner_guard.unit_text()` and
`runner_guard.main_unit_text()` as root-owned `0644` systemd units. Do not add
drop-ins or silently rewrite the text. Run `daemon-reload` before starting.

The guard unit is `sub2api-migration-recovery-20261016.service`, matching
`source_operations.GUARD_UNIT`; it uses `Restart=always`. The main unit is
`sub2api-migration-runner-20261016.service`; it invokes `runner_service.py
continue` with `Restart=no`, `TimeoutStopSec=0.4`, and `KillMode=control-group`.
The guard does not start or restart `continue` automatically.

All state is beneath root-owned private mode `0700` directory
`/var/lib/sub2api-migration-20261016/runner`. JSON files are private mode `0600`,
written through fsync and atomic rename. `guard-config.json` has exactly these
fields; additional fields are rejected:

```json
{
  "v": 1,
  "run": "20261016",
  "owner": "32 lowercase hex characters, matching source-manifest.json",
  "source_manifest_sha256": "64 lowercase hex characters",
  "role_owner_comment_sha256": "64 lowercase hex characters",
  "pg_data_host_path": "/the/exact/canonical/docker/mount/source",
  "slot_create_intended": true
}
```

`source_manifest_sha256` is SHA-256 of the **raw bytes** of
`source-manifest.json`, including its final newline. It is not a canonicalized
JSON hash. The installer must first validate the original replication role
ownership COMMENT and separately attest SHA-256 of that COMMENT's UTF-8 bytes
in `role_owner_comment_sha256`. It must independently establish that this run
created the named physical slot before asserting `slot_create_intended=true`.
Do not derive the original role owner from the new runner owner token.

The source manifest keeps its existing closed schema. Its guard unit digest
must be SHA-256 of `unit_text().encode()`. Its PostgreSQL system identifier,
timeline, exact container ID/config/image, replication role, application,
address and slot identify the only resources the guard may inspect or fence.
The data directory in PostgreSQL and the Docker mount destination are fixed
to `/var/lib/postgresql/data`; the mount source must match the config exactly,
be writable, and resolve to the same canonical existing host directory.

The core checkpoint is `state.json`, including the durable runner's existing
`window` and cumulative `pause_seconds`. The guard reads atomic checkpoints
without taking `backend-state.lock`. It owns only its nonblocking
`guard.lock`; `execution.lock` remains the runner service's process ownership
lock and is not a checkpoint lock.

## Heartbeat, WAL sampling and trips

The guard writes `guard-status.json` every 100 ms when storage permits. Its
required fields exactly match `SourceOperations._fresh()` and `_guard()`:

```json
{
  "v": 1, "run": "20261016", "owner": "...", "boot_id": "...",
  "checked_at": 0.0, "healthy": false, "pg_wal_protected": false
}
```

Additional fields report `tripped`, `sample`, `recovering`, `probe_failures`,
and a fixed reason code. Startup fails closed until the first successful
sample and a successful
main-unit supervision check. Every tick verifies the pinned main unit and its
loaded runtime policy, including while no checkpoint/window exists or the
main service has not started. An inactive unit with an empty cgroup is valid.
A changed unit or unavailable systemd check makes that heartbeat unhealthy;
successful later checks restore eligibility and clear the transient reason.
These supervisor failures do not latch a WAL trip or trigger the WAL fence;
WAL sampling, heartbeat publication, and any existing recovery child continue.
A latched WAL trip still prevents health after supervision recovers. Source
operations also require the guard process/unit to be running and the
heartbeat to be no more than 3 seconds old.

One independent worker samples local PostgreSQL and disk state every 250 ms.
A sample taking longer than 1.25 seconds cannot prove health, even if it
eventually succeeds; accepted samples expire after 1.5 seconds. The worker
cannot block heartbeat
or recovery supervision. There is at most one sampler worker, including if a
local command stalls. Every command and SQL statement has a finite timeout.

A running PostgreSQL sample requires all of:

- Exact source manifest, container/image/config, canonical mount and cluster
  system identifier/timeline; PostgreSQL remains a primary.
- The separately attested role COMMENT and restrictive replication-role
  privileges; the exact named slot is physical and not temporary.
- Effective `max_slot_wal_keep_size` between 0 and 512 MiB inclusive.
- Slot retained WAL strictly below 384 MiB and a valid restart LSN.
- At least 2 GiB available on the exact data filesystem.

A stopped PostgreSQL process proves WAL protection only with clean exit,
restart disabled, and the source operations ledger's owned SIGINT intent,
valid arm ID and final LSN. If psql loses its connection during an owned
shutdown, the worker rechecks this complete stopped-resource proof; a
still-running, crashed or foreign-stopped process fails the sample.

Every failed or late sample immediately clears the previous sample and makes
the heartbeat unhealthy. A single bounded command timeout, unavailable Docker
client, failed probe command or busy PostgreSQL sampling lock does not by
itself destroy replication. These fixed transport reason codes accumulate
completed failures. At least three consecutive failures lasting at least
three seconds latch `guard-trip.json`; only a new, timely, valid sample resets
that transient episode. Heartbeat ticks do not count as failed samples.

An independent four-second deadline from startup or the last valid sample
also latches a trip. This bounds loss of observation even when only one worker
hangs or fewer than three attempts complete. No replacement sampler starts
while that worker remains alive. The deadline leaves margin for the existing
heartbeat and bounded systemd check. A malformed sample, invalid worker clock,
explicit identity/ownership violation, unbounded WAL cap, excessive retained
WAL or low disk still trips immediately, including a late worker result that
reports such a violation. Disk pressure is checked before the SQL probe, so
a SQL timeout cannot hide a disk violation already observed by that worker.

Probe errors preserve fixed reason codes such as `GUARD_COMMAND_TIMEOUT`,
`GUARD_PG_SAMPLING_BUSY` and `GUARD_ROLE_NOT_OWNED`; raw command arguments,
SQL and stderr are never persisted. A trip during an unavailable episode
also records its failure count, first failure time and latest probe reason.
The absolute deadline uses `GUARD_PROBE_UNAVAILABLE_DEADLINE` while retaining
the underlying probe reason. Startup and all source-stop gates continue to
require a fresh healthy sample; transient tolerance grants no migration
authority. The eight-second admission budget and recovery supervisor run
independently throughout a transient episode. A successful sample never
clears an existing trip, including after a guard restart.

A separate worker attempts
the fixed fence at most 3 times; each worker attempt itself performs at most
3 bounded revalidation/SQL cycles. It never stops/restarts production
PostgreSQL, the application, Redis, Caddy, or the shared migration channel.

The fence revalidates cluster, mount and role ownership, then uses fixed SQL
to set only the migration role `NOLOGIN`, terminate only its exact owned
walsender (role, application and source address), and drop only the named
inactive physical migration slot. Ownership is checked again inside the
same transaction as the role change. A successful fence requires a fresh
read proving `NOLOGIN` and no slot, then fsyncs `guard-fence.json`. Unknown
clients, an active slot that does not drain, identity changes, or failed
ownership proof prevent an unsafe broad kill/drop.

A successful fence does not clear the trip or automatically rearm migration.
After a guard restart the trip remains latched. If disk pressure prevents
writing the trip, the guard still runs the independent fence and retries
persistence. If heartbeat persistence fails, it keeps the fence worker alive
and lets the old heartbeat expire instead of advertising health. Operators
must investigate and explicitly restore/re-attest safe state before removing
any trip; the program contains no reset command.

## Recovery takeover

The supervisor considers the cumulative budget across all windows, attempts
and process restarts. It takes over if the main process is absent or if the
current window has 0.5 seconds or less remaining from the total 8-second
internal budget. With no earlier pause, takeover begins at 7.5 seconds and
retains 2.5 seconds before the user limit of 10 seconds. Monotonic time is used on the same boot; after reboot,
backwards/uncertain wall time triggers immediate conservative takeover.

Takeover sends a nonblocking stop only to the fixed main unit. After 600 ms,
if necessary, it sends SIGKILL to that unit's entire cgroup. It never sends a
signal to a business container or the data channel. It verifies the exact
unit file, no drop-ins, loaded fragment, restart/kill policy, no pending
daemon reload, and the fixed cgroup identity. Recovery cannot start until
`MainPID=0`, `Job` is empty and cgroup v2 `cgroup.events` has `populated 0`
(or the fixed cgroup no longer exists).

Only then does it spawn the fixed command:

```text
/usr/bin/python3 -I /usr/local/libexec/sub2api-migration-20261016/runner_service.py recover
```

There is at most one live recovery child. The parent never waits for network
I/O in that child: WAL sampling and heartbeats continue. The runner backend
must still enforce finite network timeouts. If recovery exits while the
window remains open, the guard starts another `recover` with the same
checkpoint and ledgers after 100 ms. It stops retrying when the checkpoint
shows the window closed; it does not independently resume admission or
reverse any accepted authority.

## Validation and live prerequisites

The offline suite injects time, sampling failures, disk persistence failures,
process/cgroup state, recovery hangs, cluster/role/slot identity changes and
shutdown races:

```sh
python3 -m unittest discover -s scripts/migration -p runner_guard_test.py -v
python3 -m py_compile scripts/migration/runner_guard.py scripts/migration/runner_guard_test.py
```

Before retiring the original source monitors, the installer must verify a
fresh healthy guard and its pinned unit with the actual AWS command latency,
cgroup v2, PostgreSQL permissions, data mount, available disk and WAL
thresholds. Real stop/kill/recovery timing and the full end-to-end 10-second
user limit require the isolated migration rehearsal; offline tests do not
establish those production timing bounds.
