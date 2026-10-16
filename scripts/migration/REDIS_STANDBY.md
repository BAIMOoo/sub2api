# Redis standby preparation

`prepare_redis_standby.py` prepares an isolated Tencent standby of the current
AWS Redis 8.6.2. Default execution is an offline dry run with zero SSH commands.
`--inspect` reads both hosts. Only an explicitly reviewed `--apply` starts
replication; this helper has no promotion, FAILOVER, source configuration change,
or resource deletion path.

```sh
python3 scripts/migration/prepare_redis_standby.py
python3 scripts/migration/prepare_redis_standby.py --inspect
python3 scripts/migration/prepare_redis_standby.py --apply --expected-source-run-id REVIEWED_RUN_ID
```

The exact source image Config digest and seven ordered RootFS DiffIDs must match
the previously imported target image. Source channel identity must match the
freshly inspected source run ID and replication ID. Source persistence must be
healthy and idle, both filesystems need at least 2 GiB free, source needs fork
memory margin, and target needs at least 512 MiB available. The new container has
a staging memory limit of 256 MiB and no swap; this is not a final production
sizing decision. Authentication-bearing configurations deliberately fail closed.

If the source's replication backlog was initially inactive, Redis creates a new
replication ID when the first new replica connects. This is expected Redis
8.6.2 behavior, documented in
[`createReplicationBacklogIfNeeded`](https://github.com/redis/redis/blob/8.6.2/src/replication.c#L342).
Preparation accepts only that absent-to-active backlog transition with the same
source process run ID, primary role and `no-failover` state, then pins the new
active replication ID. An already active replication epoch cannot change. The
source channel's initial inspected ID still must match before resources are
created. A new prepare refuses preexisting source replicas or failover activity.

The new container is `sub2api-migration-redis-standby-20261016`, its separate volume
is `sub2api-migration-redis-data-20261016`, and private preparation evidence is in
`/home/ubuntu/sub2api-migration-20261016/redis-standby`. It binds only target
`127.0.0.1:26379` and follows the existing encrypted source channel at
`127.0.0.1:16379`. It preserves the source RDB schedule, disabled AOF and noeviction
policy. It cannot serve stale data and remains a read-only replica.

The actual runtime configuration is `/data/migration.conf` in the owned persistent
volume. Setup explicitly owns both `/data` and the config as the Redis UID/GID
from the exact source image, and preserves the official entrypoint. New official
entrypoints may skip permission fixes when they see an unfamiliar data file;
retaining the entrypoint alone does not prove writability. Apply verifies the
runtime config path and executes `CONFIG REWRITE` on this target replica without
changing its role. Later role handover must explicitly rewrite this config, so a
restart cannot restore the obsolete AWS `replicaof` setting. A private initial
audit copy is retained separately and is not the file Redis reads.

The exact source image's official entrypoint also auto-loads its default Redis
modules on every start. `CONFIG REWRITE` writes those modules into the config;
restarting the unmodified command would then load them twice and abort. A small
owned startup shell removes only parameter-free `loadmodule` lines from the
image's default module directory before delegating to the original entrypoint.
It preserves the role and every other setting, and the original entrypoint still
loads all default modules and drops privileges. Custom module paths or arguments
cause refusal. The real local restart test covers this compatibility behavior.

The advertised replica address is **172.18.0.1:26379**. It is the source Docker
bridge address, not target loopback. Initial replication can be prepared before
the reverse listener exists. Final coordinated FAILOVER must not be attempted
until all of these separately owned prerequisites are verified:

- Source `172.18.0.1:26379` forwards through encrypted SSH to target
  `127.0.0.1:26379` and returns the exact owned standby's Redis run ID.
- Source firewall permits this listener only from the inspected source Redis
  container IP; other Docker clients and public interfaces are blocked.
- The restricted source-held SSH key permits exactly `127.0.0.1:26379` as an
  additional destination. Its existing permissions and channels are preserved.
- Source Redis reports the standby under that announced bridge address; offset,
  global writer quietness and the role-switch runbook have all passed review.

This helper does not implement those network changes or the role switch.

The target scope is created exclusively. Existing scopes, containers or volumes
cause refusal. After creating the volume, setup rechecks its random owner/run
labels, local driver and empty content before writing or mounting it; a named
volume created by another actor between preflight and creation is not adopted.
The created container's owner, image and command are checked before start.
Failures retain evidence and any owned resources. Re-running `--apply` after a
partial or successful run intentionally refuses; it does not reset replication,
erase data, adopt an existing instance or retry an unknown outcome. Review the
private owner manifest and actual target state before planning explicit recovery.
There is no automatic cleanup command, and source authority remains unchanged.

Local verification:

```sh
python3 scripts/migration/prepare_redis_standby_test.py
SUB2API_REDIS_STANDBY_DOCKER_IMAGE=redis:8.4-alpine python3 scripts/migration/prepare_redis_standby_test.py
```

The opt-in test uses an already available local official Redis image, unique
disposable resources and `network=none`. It proves Redis can rewrite the volume
config and that a simulated role change plus saved data survive a restart. It
cannot reach AWS or Tencent and cleans its container/volume afterward. This test
does not establish exact Redis 8.6.2 remote image startup, cross-cloud replication,
live tunnel reachability, final sizing or production failover readiness; those
remain separate execution checks.
