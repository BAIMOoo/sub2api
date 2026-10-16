# Fixed AWS entry bridge activation

`activate_entry_bridge.py` makes at most one durable attempt to insert the
reviewed AWS loopback NAT rule. This is only the entry activation step. A
`COMPLETE` result means that the entry rule and unchanged source processes were
verified; it does not mean that PostgreSQL, Redis, DNS or migration has completed.

This file and its tests were prepared locally. They do not authorize or perform
a remote production invocation. The migration leader owns installation, evidence
acceptance, production execution, drain and all later routing decisions.

## Fixed effect

The sole mutation command the driver can invoke is:

```sh
/usr/sbin/iptables -w 1 -t nat -I OUTPUT 1 -p tcp -d 127.0.0.1/32 --dport 8081 -m owner --uid-owner 997 -m conntrack --ctstate NEW -m comment --comment sub2api-live-migration-20261016 -j REDIRECT --to-ports 18081
```

The canonical JSON rule-argument SHA256 is
`61925fd96c25a75f2fbcd84ca1c6ac1a0ce7c3646d83066a973c13bb380d7e92`.
Before loading the installed controller, the driver uses Python AST literal
parsing to check that `runner_service.py` has this exact `COMMENT` and `RULE`.
It also checks the imported values. It never evaluates expressions from the
controller during static rule verification.

The rule must be first in OUTPUT, and the unique rule with the owned comment
in the whole NAT table. The full pre-insertion NAT snapshot is retained.
Removing just the owned row from the inspected NAT listing must recover every
previous row in its previous order. The driver never flushes a table, restores
a snapshot, deletes a rule or changes conntrack.

Only new matching Caddy upstream TCP connections are redirected. Existing TCP
connections, including streams, are not deliberately closed. Existing keepalive
connections, SSE and WebSocket sessions still need the controller's source
application drain; insertion alone does not prove all requests have moved.
No Caddy, Codex relay, migration tunnel or container lifecycle command exists
in this driver.

## Trusted installation and invocation

The AWS installation location is fixed:

```text
/usr/local/libexec/sub2api-migration-20261016/activate_entry_bridge.py
```

Install the reviewed file as a regular, single-link `root:root` file with mode
`0644`, alongside the accepted `runner_service.py`, `runner_backend.py`,
`source_operations.py`, `durable_runner.py` and `target_rpc.py`. Each dependency
must meet the same file policy. The library directory and all ancestors must
be root-owned directories without group or world write access and without
symlink components. Do not change dependencies to make this invocation pass.

Running with no arguments, from any location, is offline and only emits a
description and rule hash. It does not import the installed controller, inspect
hosts, open sockets or invoke commands:

```sh
python3 scripts/migration/activate_entry_bridge.py
```

Only the Linux root process at the exact installed path can select either live
mode. The leader invokes the installed driver with isolated Python:

```sh
/usr/bin/python3 -I /usr/local/libexec/sub2api-migration-20261016/activate_entry_bridge.py apply
```

Read or reconcile a persisted attempt with:

```sh
/usr/bin/python3 -I /usr/local/libexec/sub2api-migration-20261016/activate_entry_bridge.py inspect
```

Both live modes hold `runner_service.execution_lock()`, preventing the
cooperating main controller from starting a data window during activation.
This does not exclude unrelated root processes or firewall editors.

## First-attempt prerequisites

The driver performs the full preflight twice and requires the accepted gate
hash, owner, source machine/boot, source resource IDs/PIDs and protected service
snapshots to remain equal across them. Each preflight requires:

- Source authority remains AWS for both PostgreSQL and Redis; no active data
  window; `route_fenced` is false.
- The main migration unit is inactive with PID zero and no queued systemd job;
  the controller fault selftest is disarmed; the independent guard passes its
  existing freshness and health checks after remote probes.
- Root's accepted deployment gates bind this run/owner, the fixed application
  image and binary, rehearsal/model-canary/TLS evidence hashes and source
  deployment-path fencing.
- The original source app, PostgreSQL and Redis containers are running; the
  source app restart policy is `no`.
- `caddy.service`, `codex-remote-relay.service` and
  `sub2api-migration-channel-20261016.service` are active/running with no queued
  jobs. Their PIDs and monotonic start timestamps are captured. All four Caddy
  UID values in `/proc/<pid>/status` must equal 997.
- Tencent authority remains AWS/AWS, PostgreSQL is in recovery without a
  promotion arm/intent, Redis is a replica, and persisted app endpoints still
  designate the source services.
- Tencent's app is ready and serving without drain, PostgreSQL/Redis admission
  is unpaused, Redis cache/pubsub is ready, and all background workers remain
  disabled and unactivated.
- The AWS loopback bridge responds successfully to `/health` on port 18081.
  The driver does not send another paid model canary or test the public TLS
  endpoint itself. Root's accepted gate file is the trust boundary for those
  already reviewed checks.

## Persistence, uncertain results and replay

The fixed private scope is:

```text
/var/lib/sub2api-migration-20261016/runner/entry-bridge
```

The scope and runner parent are root-owned mode `0700`; snapshots and journal
files are root-owned mode `0600`, without symlinks or multiple hard links.
The driver uses the accepted source helper's atomic replace, file fsync and
directory fsync operations. It retains:

- `before.json`: the full first accepted preflight proof.
- `before-nat.rules`: the complete previous NAT listing.
- `state.json`: owner/run/rule hash, prior proof/NAT hash, `attempted: true`,
  and an `INTENT`, `UNKNOWN` or `COMPLETE` phase.

Snapshots are written first. A second full preflight and unchanged NAT check
follow. `INTENT` is durably saved before the one insertion command. If a crash
occurs immediately after saving intent and before executing the command, this
can leave no bridge installed; that conservative ambiguity still prevents a
second attempt.

After insertion, including a timeout or nonzero command result, the driver
inspects rather than repeating the mutation. All later `apply` invocations
with an existing journal also inspect only. Inspection requires the saved NAT
snapshot hash, first/unique fixed rule, every other NAT row and all protected
service/source resource identities to match. Successful inspection records
`COMPLETE` with `VERIFIED`. Failure records `UNKNOWN` with a fixed reason code;
unexpected external exception text is not included. A failed reinspection
clears any prior successful after-snapshot hash and current unchanged flags.

A command can affect the kernel even if its receipt is lost. A process can
also fail while writing the result after the insertion. Preserve all journals
and snapshots and reconcile with `inspect`. Never delete the journal, reinsert
by hand or treat a local exit code as proof that the kernel was unchanged.
`UNKNOWN` requires operator inspection; it does not trigger rollback.

Whole-table before/after comparison detects other firewall changes but cannot
atomically prevent an unrelated editor between the final read and insertion.
The driver intentionally avoids restoring the whole table in that situation.
The rule may disappear on reboot. The driver does not install an iptables
persistence mechanism; the leader owns reboot and routing lifecycle decisions.

## Restricted manual removal reference

This driver has no delete path. The following is a reference for the migration
leader, not an automatic recovery step. Before considering it, establish fresh
source and Tencent proof that both authorities remain AWS/AWS, the original
source app is alive, the controller is stopped with no active data window, and
the implications for current/new requests have been reviewed. After any data
authority commit, do not blindly delete the bridge or send traffic back.

If those conditions hold and the leader specifically chooses removal, delete
only the exact fixed rule:

```sh
/usr/sbin/iptables -w 1 -t nat -D OUTPUT -p tcp -d 127.0.0.1/32 --dport 8081 -m owner --uid-owner 997 -m conntrack --ctstate NEW -m comment --comment sub2api-live-migration-20261016 -j REDIRECT --to-ports 18081
```

Capture before/after evidence. Do not flush chains, use ordinal deletion,
restore the old table or clear connection tracking. Removal does not reverse
existing connection mappings or substitute for the controller's drain policy.

## Local verification boundary

```sh
python3 -m py_compile scripts/migration/activate_entry_bridge.py scripts/migration/activate_entry_bridge_test.py
python3 -m unittest discover -s scripts/migration -p activate_entry_bridge_test.py -q
```

The offline fault suite covers the fixed rule/command, source/target gates,
durable ordering, lost receipts, no-effect uncertainty, replays, concurrent NAT
changes, protected process changes, snapshot tampering and sanitized error
evidence. It invokes no SSH, systemctl, iptables or network sockets. These tests
do not prove live Linux NAT behavior, real Caddy stream continuity or the
production ten-second admission budget.
