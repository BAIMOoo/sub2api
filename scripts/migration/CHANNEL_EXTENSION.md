# Extend the preparation SSH channel before replicas start

`extend_channel.py` defaults to an offline dry run. `--inspect` reads both hosts
using independent management connections, and does not create state. The Tencent
connection uses the Mac's HTTP CONNECT proxy and Tokyo key, independently of AWS.
Only reviewed `--apply` mutates the dedicated migration channel. Do this step
before starting PostgreSQL/Redis replication: restarting the original channel
briefly disconnects every forward inside it. The helper refuses if replication
slots, WAL senders, Redis replicas, or either new target standby container exist.

The AWS application, PostgreSQL, Redis, business Caddy and Codex remote webpage
service are not restarted or reconfigured. No DNS, business data, sshd policy,
image or application deployment changes occur.

| Listener | Destination | Status |
| --- | --- | --- |
| AWS 127.0.0.1:18081 | Tencent 127.0.0.1:8082 | Preserved |
| Tencent 127.0.0.1:15432 | AWS PostgreSQL 172.18.0.3:5432 | Preserved |
| Tencent 127.0.0.1:16379 | AWS Redis 172.18.0.2:6379 | Preserved |
| AWS 172.18.0.1:26379 | Tencent 127.0.0.1:26379 | Added; only AWS Redis container may connect |
| AWS 127.0.0.1:18092 | Tencent 127.0.0.1:18092 | Added for private app control |
| AWS 127.0.0.1:25432 | Tencent 127.0.0.1:25432 | Added for later AWS recovery replica |

The original unit must match the original generator exactly, with no systemd
drop-ins. The source Redis bridge IP/gateway must remain 172.18.0.2/172.18.0.1.
The actual Linux bridge interface is inspected rather than guessed. New source
ports must be entirely unused. Both old reverse data forwards must pass real
PostgreSQL SSL negotiation and Redis PING before any changes occur.

Only the authorized_keys line with the exact source-held migration Ed25519 public
key is extended. Existing `from`, `restrict`, `port-forwarding`, `/bin/false`
forced command, original permitopen and both permitlisten options remain. Exactly
three permitopen destinations are appended: target loopback ports 26379, 18092,
25432. Duplicated keys or drifted restrictions are refused. Other keys and their
bytes are preserved, including later unrelated edits during restoration. The
private key stays on AWS and is never read or copied by this helper.

The source INPUT guard has exactly two owned rules: allow TCP to bridge
172.18.0.1:26379 from 172.18.0.2 on the inspected bridge interface, followed by a
REJECT for every other source to that destination/port. Each rule has an exact
`sub2api-channel-extension-20261016` comment. No chain is flushed and no whole
firewall table is restored. The dedicated channel's ExecStartPre invokes a
root-owned firewall helper before SSH can open the new bridge listener. This
reestablishes the guards after a host reboot and fails closed if addressing or
ownership changes. Guards are reordered only while that listener is closed.

```sh
# Zero SSH calls.
python3 scripts/migration/extend_channel.py

# Read-only, including independent Tencent management access.
python3 scripts/migration/extend_channel.py --inspect

# Only after the leader's review, while no replica uses the channel.
python3 scripts/migration/extend_channel.py --apply

# Offline restoration plan.
python3 scripts/migration/extend_channel.py --restore

# Recover this exact owned extension, still before replicas start.
python3 scripts/migration/extend_channel.py --apply --restore
```

Private local state defaults to
`~/.local/share/sub2api-migration-20261016/channel-extension`. AWS remote state is
under `/home/ubuntu/.local/share/sub2api-migration-20261016/channel-extension`,
and Tencent state is under
`/home/ubuntu/sub2api-migration-20261016/channel-extension`. Directories are 0700;
old unit, stored unit, complete authorized_keys snapshots, firewall audit and
owner/phase records are 0600. The boot firewall script is 0700 and root-owned.
Local and per-host locks serialize a mutation with unknown-result recovery.
Existing state is retained and is never implicitly adopted for a new run.

Mutation order is target key backup/extension, then source unit backup/extension
and channel restart. All original and added source listeners must have exact
bind addresses. Existing target reverse forwards must again pass PostgreSQL and
Redis probes. Independent Tencent management SSH must still succeed. If the
target API on 8082 was serving during preflight, its HTTP health must also remain
the same through AWS 18081. If it was absent, this phase proves the preserved API
listener/forward command; it cannot prove a nonexistent app's HTTP response.
New target services may also be absent: this phase checks the three added source
binds, key permissions and firewall guard, not their future destination health.
Before coordinated Redis FAILOVER, positively verify from the actual source
Redis container that the bridge route returns the exact owned Tencent Redis run
ID. That proof is required separately once the standby starts.

On failure, automatic recovery attempts these steps:

1. Acquire the source mutation lock and verify the private owner manifest,
   machine identity, replica absence, and exact before/after unit content.
2. Stop only the migration channel, restore its two exact old unit copies,
   reload systemd and start the original unit. Confirm the old loopback API
   listener returned and every new bridge/control/database listener is closed.
3. Remove only the two exact owned firewall rules, after the bridge listener
   is proven closed. If an operator changed the unit or restoration cannot
   close the listener, retain the firewall and refuse further source changes.
4. Restore only the target migration key line's original options; preserve
   every unrelated key. Recheck management SSH and both original data forwards.
5. Retain all private backups and phase records for review. A lost controller
   can use explicit `--apply --restore` after its local/remote locks release.

If automatic restoration reports incomplete, inspect the three private state
directories first. Do not delete guards, reset whole firewall tables, overwrite
the full live authorized_keys backup, restart business containers, or reseed
databases as an attempted channel repair. No cleanup path removes backup state.
After replicas begin using the channel, this preparation-only restore procedure
is intentionally refused; a separately reviewed maintenance handover is needed.

Offline validation:

```sh
python3 scripts/migration/extend_channel_test.py
python3 -m py_compile scripts/migration/extend_channel.py
```

At author delivery, 16 offline tests and an actual read-only inspection passed.
Source bridge was `br-b1f90bc59054`; both original data forwards passed, no target
8082 service was running, and neither replica had been prepared. No apply,
firewall change, key edit or unit restart was executed by the author. Mock tests
do not prove actual systemd privileged ExecStartPre behavior, firewall packet
filtering, or live expanded-channel recovery; those remain reviewed execution
checks. The bind-only preparation result is not production cutover readiness.
