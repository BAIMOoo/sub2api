# AWS 源端固定操作后端

`source_operations.py` 是迁移 runner 的本机库，直接执行没有副作用，只输出离线信息。它不安装服务、不连接 SSH、不修改入口/DNS、不提升目标 PostgreSQL，也没有启动旧主端的 API。正式调用由 root runner 负责；该库不自行决定切换时机、排队截止时间或主权回退。

生产路径固定：`/var/lib/sub2api-migration-20261016/runner/`。文件必须 root 所有、普通文件、单硬链接、0600；目录 0700，祖先目录必须 root 所有且组/其他用户不可写，拒绝 symlink。程序应随 runner 安装在 root 拥有且不可由普通用户替换的固定 libexec 目录。

## 清单与证明

`source-manifest.json` 使用精确字段集合，未知字段拒绝：

```json
{
  "v": 1,
  "run": "20261016",
  "owner": "<32 lowercase hex>",
  "machine_id_sha256": "<SHA256 of stripped /etc/machine-id bytes>",
  "containers": {
    "app": {
      "id": "<64 lowercase hex container ID>",
      "config_sha256": "<SHA256 of canonical Docker Config object>",
      "image": {
        "config_digest": "sha256:<64 lowercase hex>",
        "diff_ids": ["sha256:<ordered uncompressed RootFS DiffID>"],
        "os": "linux",
        "architecture": "amd64"
      }
    },
    "postgres": "<same object shape>",
    "redis": "<same object shape>"
  },
  "postgres": {
    "system_identifier": "7626048206782038050",
    "timeline": 1,
    "replication_user": "sub2api_mig_20261016_replica",
    "replication_application": "sub2api-pg-replica-20261016",
    "replication_address": "172.18.0.1",
    "slot": "sub2api_mig_20261016"
  },
  "redis": {
    "source_run_id": "<40 lowercase hex>",
    "source_replid": "<40 lowercase hex>",
    "target_run_id": "<40 lowercase hex>"
  },
  "units": {
    "sub2api-pg-replica-20261016-source-monitor.timer": "<SHA256 of exact root unit bytes>",
    "sub2api-pg-replica-20261016-source-monitor.service": "<SHA256>",
    "sub2api-pg-replica-20261016-source-monitor-fence.service": "<SHA256>",
    "sub2api-migration-recovery-20261016.service": "<SHA256>"
  }
}
```

JSON 摘要算法：UTF-8、键排序、紧凑分隔符 `(',', ':')`、`allow_nan=False`，与模块的 `canonical` 一致。三个资源名固定为 `sub2api`、`sub2api-postgres`、`sub2api-redis`。每次现场操作重新核实 machine-id、不可变容器 ID、Docker Config 摘要及镜像 Config 摘要、有序 RootFS DiffIDs、OS/architecture；跨 Docker classic/containerd 存储不能仅比较本地镜像 `.Id`。所有 exec/update/signal 使用清单中已验证的完整容器 ID，防止同名容器替换导致误操作。

runner 写入 `source-attestations.json`：

```json
{
  "v": 1,
  "run": "20261016",
  "owner": "<manifest owner>",
  "boot_id": "<current /proc/sys/kernel/random/boot_id>",
  "checked_at": 1792166400.0,
  "app_route_fenced": true,
  "postgres": {
    "writers_quiet": true,
    "final_lsn": "8/CA000000",
    "target_replay_lsn": "8/CA000000",
    "arm_id": "<32 lowercase hex>"
  },
  "redis": {"writers_quiet": true, "consumers_paused": true}
}
```

证明必须来自同一 owner/boot，时间为有限数，不能在未来，不能超过 3 秒。它是 runner 的受信证明，不是 source helper 自行从摘要推断目标已排空/重放的机制。runner 必须先验证真实目标端状态，再写入证明。

独立恢复服务写入 `guard-status.json`，字段为：

```json
{"v":1,"run":"20261016","owner":"<manifest owner>","boot_id":"<current boot>","checked_at":1792166400.0,"healthy":true,"pg_wal_protected":true}
```

源 PG 旧监控退役与交接前，必须同时验证 guard 固定 unit 文件摘要、没有 DropInPaths、有效 FragmentPath、active/running、MainPID>0、没有 systemd Job，以及上述新鲜状态。仅仅“进程存在”不能证明它正在保护 WAL。

`systemctl show` 对固定源 PG monitor timer 不提供 `MainPID` 属性；仅该名单内 timer 缺少该字段时，将其规范为 `MainPID="0"`，其余状态字段及重复字段检查保持严格。三个 service 单元仍必须提供实际 `MainPID`，不得合成 PID。

## 固定 Python API

```python
ops = SourceOperations()                 # 默认本机 root OS 适配器
ops = SourceOperations(fake_backend)     # 仅供离线故障测试
```

调用方不提供 SQL、DSN、容器名、文件路径、systemd unit、Redis 命令或任意 argv。唯一可变操作参数是 `op_id`（32 小写十六进制）和 Redis `timeout_ms`（整数 100..6000，禁止 bool）。业务端口固定为容器内 8080，协调 Redis 目标固定为 `172.18.0.1:26379`。

| API | 结果与条件 |
| --- | --- |
| `resource_fences()` | 恢复路径专用只读摘要，仅返回 `v/run/owner/checked_at/resources`；三个资源摘要字段与 `inspect()` 完全一致，每个仍完整验证 machine-id、固定容器 ID、Config、镜像 Config 与 RootFS 身份。无 unit、PG SQL、Redis ROLE/INFO/客户端采样及 ledger 写入。摘要保留异常退出/OOM/restart 字段，由恢复编排判断，单独不能证明当前数据主权或授权任何变更。 |
| `inspect()` | 无持久状态写入。返回 `resources.app/postgres/redis` 的 `id/running/status/restart/exit_code/oom_killed`，`app_connections`，units，已存 operations；PG 运行时含 `postgres`，Redis 运行时含 `redis.source/target` 双端 INFO 与源连接样本。Redis 自检只设置本次短连接名称，不修改业务数据/角色。 |
| `disable_app_restart(op_id)` | 单独执行并验证 `restart=no`，保持旧应用运行。必须在腾讯应用首次共享 AWS Redis 前执行。丢失 docker update 响应后通过实际策略恢复。 |
| `app_stop(op_id)` | restart=no、route_fenced 新鲜证明、源容器网络 namespace 中所有业务入口 TCP 连接自然归零后，仅一次 SIGTERM。检查所有 live inbound 状态，含 idle keepalive、SSE、WS、CLOSE-WAIT；LISTEN/TIME-WAIT 忽略。PG/Redis 出向池单独计数，允许退出清理时仍存在。返回 pending 时继续 inspect/同 ID 轮询，不升级为强杀。 |
| `retire_pg_monitors(op_id)` | guard 已接管保护后，disable/stop 精确旧 timer；等待旧 monitor/fence 无 MainPID、无 Job、inactive；失败或仍在重试的 fence 不能被强制消除。将精确已验 unit 文件备份至 root scope，并原子替换为 `/dev/null` 持久 mask，daemon-reload，再核实三者 idle/masked。 |
| `pg_final_evidence()` | 固定只读 SQL，返回 `system_identifier/timeline/in_recovery/flushed_lsn/prepared_transactions/sessions/unknown_sessions/owned_replication/replication/slots/wal_cap_mb/checked_at/boot_id/quiet`。`quiet` 要求无未知 session 且 prepared transactions=0。idle 客户端也阻断。仅精确匹配复制角色、application/address 的 walsender 可排除；不输出 SQL 查询文本。 |
| `pg_stop(op_id)` | 旧 app 正常退出、guard 新鲜保护、旧监控已退役、writer quiet、target_replay_lsn>=final_lsn、实际 PG 身份/主角色与未知会话检查通过后，先 restart=no，再一次 SIGINT fast shutdown。返回 `phase=pending/complete`、`stopped`、`authority=UNKNOWN/SOURCE_FENCED`，保留 final_lsn/arm_id/证据摘要。 |
| `redis_roles()` | 只读双端 ROLE/INFO，返回 `{source:INFOdict,target:INFOdict}`。源 Redis 停止后不得依赖该通路；目标 RPC 仍独立可用。 |
| `redis_failover(op_id, timeout_ms)` | 旧 app/PG 已退出、guard 正常、writer/consumer quiet、源目标 run_id/replid 正确、唯一在线反向复制地址正确、两端业务客户端物理关闭后，一次 `FAILOVER TO 172.18.0.1 26379 TIMEOUT <budget>`，没有 FORCE。先禁源 Redis 自动重启。首次通常 pending，同 ID 重查两端 ROLE/INFO后 complete/TENCENT 或 unknown/UNKNOWN，不重复发角色命令。 |
| `redis_role_persist(op_id)` | 交接已确认、源为目标的在线 replica、目标为 master 后，一次 SIGTERM 正常停止旧 Redis，保持 restart=no，写 `source_role=RETIRED_STOPPED`、`authority=TENCENT` tombstone。目标 CONFIG REWRITE 和重启后 master 验证由目标 RPC 完成。 |

成功的变更 API 返回 root ledger 中完整的操作记录：`request`、`phase`、`steps`、`created_at/checked_at` 以及上述动作字段。`phase` 为 `intent/pending/unknown/complete`；`Refusal` 表示必须核实的前提不满足，`Unknown` 表示外部命令结果不明，异常文字仅包含固定错误码。

连接识别默认拒绝未知客户端。Redis probe 排除当前唯一同名自检连接，源 master 只允许精确 bridge 地址的复制连接；目标 replica 只允许精确 `127.0.0.1:16379` 上游连接；交接后检查对应反向复制。来自 bridge 的普通 idle 连接不在白名单。runner 的 Redis pause/endpoint pool 切换必须真实关闭业务连接，再开始 failover。

双端 Redis ROLE/INFO 采样使用封闭只读批次：先完整验证源 Redis 容器与镜像身份并固定正 PID、StartedAt；源端与精确目标各执行一次固定 `INFO` 和 `ROLE`，随后重新完整验证同 ID/PID/StartedAt。仅提取 INFO 的 Server、Replication 两节，保留原证据字段集合；仍校验两个进程 run ID、INFO 与 ROLE 一致、failover 状态，以及调用方已有的 replid/复制方向约束。正常存储分支由 18 个本机进程减至 8 个，不跨调用缓存身份。`_redis` 的变更与客户端采样路径仍逐次完整验证，原信号/FAILOVER intent 与重放规则保持不变。

PG guard 与源最终会话采样共用 root0600 `runner/pg-sampling.lock`，仅序列化本机固定 SQL 的连接生命周期，等待锁上限0.75秒。锁定期间等待 docker exec 完整退出，避免把 guard 的短暂检查连接误判为业务连接；不白名单 psql、application_name 或普通 idle session，其他未知会话仍阻断交接。

## 断点恢复与停止约束

`source-operations.json` 是单独 ledger。动作 intent 和每个信号/FAILOVER step intent 在命令之前 fsync 原子写入并 fsync 父目录；外部命令执行后才记录 accepted。整个本地动作使用独立非阻塞 flock，不占用 runner/guard 状态锁。重试相同 op_id 和参数只能现场 reconcile；同 ID 不同请求、同动作换新 ID 都拒绝，防止绕过一次执行约束。

SIGTERM/SIGINT/FAILOVER 一旦写入 step intent，超时、异常、进程重启、丢失响应均不能再次发命令。即使没有观测到角色变化，也必须保持 unknown 或 pending，由 runner 保守恢复。没有“超时视为失败”“延迟强杀”“自动启动旧 primary”的分支。

PG/Redis 只有 exited、ExitCode=0、没有 OOMKilled、restart=no 才能构成干净源端 fence。源端 Redis 旧规格没有已证明可持久保存 replica 角色的 config_file，因此本实现选择停止加 tombstone；不能宣称运行中的旧源角色已 CONFIG REWRITE。任何手动启动、Docker compose recreate、开机自动化重新创建旧主端的路径，都须由部署侧禁止并纳入退役验收。

本库不保证单独执行即可满足 10 秒用户等待目标。runner 必须在 quiet 窗口之外完成入口、监控交接和应用排空，并为源停机、目标提交与恢复预留足够预算。两个窗口累加等待仍由 runner 验证；不可逆意图已产生时只能向新主恢复，若超过等待目标必须记录违约，不能重启旧主伪装成功。

## 验证

```sh
python3 -m unittest scripts/migration/source_operations_test.py
python3 -m py_compile scripts/migration/source_operations.py scripts/migration/source_operations_test.py
```

离线测试覆盖身份变更、容器镜像存储差异、未排空连接、未知 idle 客户端、prepared transactions、guard 失效、在途 monitor/fence、一次信号、FAILOVER 后响应丢失、未知角色、持久 fence，以及禁用旧主自动重启。测试不会访问 AWS、腾讯或 Cloudflare；真正 PostgreSQL/Redis、systemd、流式请求和跨云截止时间仍需 runner 的隔离完整演练与正式 fresh preflight。

源 Redis 退役后若 runner 尚未保存最终主权，重放同一 `redis_failover` 操作只允许使用已确认的 TENCENT ledger、已验证角色摘要、owned SIGTERM intent 和实际干净停止/restart=no 证明。它可完成丢失停止响应后的 retirement tombstone，返回 `source_retired=true` 与 `target_verification_required=true`，不会在已停容器内执行 Redis 客户端。编排层必须紧随独立目标 RPC 核实当前 master 和目标 run ID；旧 ledger 不能代替当前目标身份/角色证明。
