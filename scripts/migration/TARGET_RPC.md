# 腾讯固定动作 RPC（20261016）

本文是 `target_rpc.py` 的安装与协议契约。脚本默认完全离线，不连接 Docker、网络、数据库或远端，也不读取安装配置。本文件不授权远端安装或数据切换。

## 入口与权限

- 无参数：输出离线说明，退出 0。
- `serve`：只读取一个 UTF-8 stdin JSON，必须关闭 stdin。总输入最多 16 KiB，包含尾随空白；读 EOF 最多 3 秒。不接受重复键、未知键、非有限数字、尾随第二个请求或非 UTF-8。
- `guard-reconcile`：不读取 stdin，恢复持久接受的 PG/Redis 交接、端点持久写入及已开始的应用控制请求。
- 其他 argv 一律拒绝。请求不能提供 shell、SQL、DSN、URL、路径、容器名、环境变量或任意命令。

安装位置固定为 `/usr/local/libexec/sub2api-migration-20261016/target_rpc.py`，root 所有、0700。程序校验自身安装路径、SHA256、`/etc/machine-id` 内容去除首尾空白后的 SHA256。所有宿主祖先目录必须无 symlink，root 所有且 group/other 不可写。RPC 私有目录必须 root0700。调用者应使用固定的 `/usr/bin/python3 -I <固定程序路径> serve`，Python 隔离模式防止用户环境影响导入；SSH forced command 不转发 `SSH_ORIGINAL_COMMAND`，禁止 PTY、agent/X11 转发。SSH channel 与独立 guard 的生命周期由 root installer/runner 管理。

```text
/var/lib/sub2api-migration-20261016/rpc/             root0700
  config.json                                    root0600
  app-control.token                              root0600
  state.json                                     root0600，atomic replace + fsync
  events.jsonl                                   root0600，append + fsync
  ledger.lock / mutation.lock / op-<32hex>.lock    root0600
/var/lib/sub2api-migration-20261016/app/             root0755
  endpoints.json                                 root0644，无秘密
/var/lib/sub2api-migration-20261016/postgres-authority/
  operation.lock / authority.lock / state.json    复用 PG authority 契约
```

私有文件必须 regular、单 hardlink、owner/mode 精确匹配。端点文件也不可 symlink。安装器须先创建 `app/endpoints.json` 为 `{"v":1,"postgres":"source","redis":"source"}`，应用通过只读目录挂载读取该文件，环境变量 `MIGRATION_ENDPOINT_STATE_FILE` 指向容器内对应路径。必须挂载目录以观察 atomic replace；绑定单个旧 inode 的文件会阻碍重启后选择更新。Go 启动逻辑只读取一次。PG switch 前必须保存已确认的目标端点；Redis 仅允许下述暂停预路由例外，持久端点仍等实际目标主权确认后才保存。

## config.json（closed schema）

顶层只有：

```json
{
  "v": 1,
  "run": "20261016",
  "owner": "<32 lowercase hex>",
  "program_sha256": "<64 lowercase hex>",
  "machine_id_sha256": "<64 lowercase hex>",
  "app": {
    "container_id": "<64 lowercase hex>",
    "image_id": "sha256:<64 lowercase hex>",
    "config_sha256": "<64 lowercase hex>",
    "rootfs_diffids": ["sha256:<64 lowercase hex>"],
    "control_port": 29001
  },
  "pg": {
    "container_id": "<64 lowercase hex>",
    "image_id": "sha256:<64 lowercase hex>",
    "config_sha256": "<64 lowercase hex>",
    "rootfs_diffids": ["sha256:<64 lowercase hex>"],
    "owner": "<PG preparation 32 lowercase hex>",
    "volume_path": "/var/lib/docker/volumes/sub2api-migration-pg-replica-data-20261016/_data",
    "system_identifier": "<decimal unsigned uint64>",
    "source_timeline": 1
  },
  "redis": {
    "container_id": "<64 lowercase hex>",
    "image_id": "sha256:<64 lowercase hex>",
    "config_sha256": "<64 lowercase hex>",
    "rootfs_diffids": ["sha256:<64 lowercase hex>"],
    "owner": "<Redis preparation 32 lowercase hex>",
    "volume_path": "/var/lib/docker/volumes/sub2api-migration-redis-data-20261016/_data"
  }
}
```

`config_sha256` 针对实际运行容器 Docker inspect 的 **Config 对象**，编码为 Python `json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()`，不是 image Config，也不是 pretty JSON。`rootfs_diffids` 精确匹配 image inspect 的有序 RootFS.Layers。容器 Id 和 Image Id 均精确匹配，不接受相同标签的新容器。三个容器必须 running、restart=no、host network、无 PortBindings。

固定容器名分别为 `sub2api-migration-app-20261016`、`sub2api-migration-pg-replica-20261016`、`sub2api-migration-redis-standby-20261016`。PG/Redis 的 owner label、run label、local volume 名称/挂载点/可写挂载均须匹配。本程序不创建、adopt、替换或删除资源。安装器必须将 PG authority manifest 的最新容器 ID、镜像、owner、system identifier、timeline、owned child volume 导入配置；PG authority library 和 monitor 由对应独立安装流程固定，RPC 不从用户 home 加载其 Python 代码。

## 请求与响应

每个请求都具有这些精确公共字段：

```json
{"v":1,"run":"20261016","owner":"<32hex>","op_id":"<32hex>","expected_epoch":0,"action":"status"}
```

新请求必须使用当前 RPC ledger epoch；新变更接受后递增一次，读取动作不递增。HTTP 控制的 `control_epoch` 是应用组件自己的 epoch，与 RPC epoch 独立。读取控制也要求携带整数 `control_epoch`，但服务端 GET 不使用它。相同 op_id、完全相同请求可重放，即使其 expected_epoch 已过期；相同 ID 的不同内容被拒绝，不能覆盖原结果。所有时间戳是 UTC Unix 秒；LSN 必须 uppercase 十六进制，按 `(high << 32) + low` 比较。

| action | 附加字段 | 含义 |
| --- | --- | --- |
| `status` | 无 | 当前 PG、Redis、应用、持久端点及最近 16 个操作摘要；pg_arm只含arm_id/status/expires_at/op_id/final_lsn，pg_promotion_intent只含op_id/accepted_at，缺失时null |
| `app.quiet-snapshot` | 无 | 单次固定只读采样：runtime前后、ingress/writers/redis/postgres控制状态、fresh PG replay；不递增RPC epoch，不接受自定义控制路径或字段 |
| `app.quiesce` | 无 | 固定 ingress → writers → Redis → PostgreSQL pause，返回完整 quiet snapshot |
| `pg.route` | 无 | fresh 核验已确认的目标 PG primary，持久 PG=target，再切换已暂停的 SQL 池 |
| `redis.preroute` | 无 | 执行下述完整预路由证明，再切换暂停的 Redis 池；不改变持久 Redis 端点 |
| `app.restore` | 无 | 确认 ledger/持久端点匹配，取消旧 quiesce，再按 SQL resume → Redis rebuild/resume → writers resume → ingress resume 恢复 |
| `app.control` | `control`, `control_epoch`；仅 `.switch` 必须另有 `endpoint` | 固定 loopback 控制，下表列出 |
| `app.persist-endpoints` | `postgres`, `redis`，均为 source/target | atomic+fsync 启动端点选择；拒绝任何已持久 target→source |
| `pg.arm` | `final_lsn`, `quiet_attestation_sha256`（64hex） | 核验 standby/最终 LSN，保存 6 秒 arm，返回 arm_id、expires_at |
| `pg.cancel-arm` | `arm_id`（32hex） | 未接受提交且仍 standby 时取消；禁止撤销已接受 intent |
| `pg.commit` | `arm_id`, `source_fence_sha256`（64hex） | 接受源停机 attestation 后持久 intent，再向前提升 |
| `pg.reconcile` | 无 | 观察/完成已接受提升；无 intent 时只核验 standby |
| `redis.inspect` | 无 | 固定实例 role/run_id/replid/数值 offsets/汇总，不读取 key 数据 |
| `redis.persist-primary` | 无 | 只对已成为 master 的固定实例 CONFIG REWRITE，核验持久配置；成功结果含authority=TENCENT |

应用控制白名单：

- `runtime.status` → GET `/status`。
- `ingress.status/pause/resume`、`writers.status/pause/resume`。
- `postgres.status/pause/switch/resume`。
- `redis.status/pause/switch/rebuild/resume`。
- `background.status/activate`。

除 runtime.status，控制路径固定为 `/data/<component>/<verb>`。`.status` 使用 GET，其余 POST。ingress.pause body 为 `{}`，其他 POST 为 `{"expected_epoch":control_epoch}`，switch 追加 endpoint（source/target）。RPC先读取组件status核验epoch。switch要求持久端点与请求端点一致；唯一例外是暂停的 Redis 在协调 FAILOVER 前预路由到目标以关闭旧连接。persist target 仍要求RPC已经确认该数据authority为 TENCENT。后台激活仍要求 runner 独立证明旧应用退休，本机状态不能证明 AWS 上旧后台已停止。

`app.quiet-snapshot` 是单次跨云请求中的封闭只读批次。只校验一次固定 app 容器/image/Config 身份、读取一次固定私控 token，再依次执行 runtime、ingress、writers、Redis、PostgreSQL 的固定 GET；随后在原 PG 锁中 fresh 校验 authority manifest 并采样真实 PG，最后再次 GET runtime。仅全部 HTTP 200/字典响应、前后 runtime instance_id 一致且完整批次不超过2秒时成功。结果顶层仅 `app/pg/ingress/writers/redis/postgres`；app仍为 `{http_status,body}`，pg仍含 system_identifier/in_recovery/replay_lsn/wal_timeline，四个组件为原 status body。令牌、初始内部runtime响应、可选路径均不输出。源 runner 继续独立检查传入实例、完整writer registry、quiet/epoch/boot及包含网络往返的2秒总新鲜度；采样动作本身不声明业务已安静。

Redis 预路由要求 PG ledger 与持久端点均为腾讯，并 fresh 检查实际目标 PG 为同集群可写 primary；Redis ledger/持久端点仍为 AWS/source。实际 app instance 在完整采样中相同、serving、非 draining，HTTP/WS active=0、后台调度关闭、15 项 writer registry 完整且 active/pending/failed/dropped 均为严格整数0、quiet_controls/writer_coverage 为 true；ingress paused+quiet（内嵌 epoch 匹配）、writers held+quiet、Redis paused+quiet/active_operations=0。全采样不超过2秒，不能要求暂停时公开 ready 为 true。

该程序固定 pin 本轮源 run_id `08e111c3724844873b08ee96322628d6a2724ee3`、目标 run_id `39d29826637d6bcf89b9630612f7fd0107260394`、源 replid `6fbca65ab347b9efa6637b4c790feaa93c29d348`。只通过锁定的目标 Redis 容器读取 loopback16379/26379 INFO，仅解析 Server/Replication 节并严格拒绝这两节内重复字段（Modules 节的重复 module 行合法且不参与证明），要求源 master、唯一在线 replica 为 `172.18.0.1:26379`，目标仍 slave、上游 `127.0.0.1:16379`、link up、sync完成、无 failover、无下游。先 fsync RPC INTENT/effect_started 再 switch；runner 在调用前另先 fsync 不可逆 COMMIT_INTENT。选择目标仅关闭旧池，暂停期间不建立目标业务连接，也不改变Redis实际角色；源 FAILOVER仍独立拒绝任何未知客户端。

私有 bearer 仅从 root0600 `app-control.token` 读取，不在 stdout/日志/返回值中输出。控制响应只保留明确字段，启动失败文本归一化为 `START_FAILED`，不透传任意 HTTP 错误 body/header。204 无 body 是成功。

典型响应：

```json
{"ok":true,"code":"OK","run":"20261016","owner":"<32hex>","op_id":"<32hex>","action":"pg.commit","epoch":2,"phase":"TENCENT","result":{"authority":"TENCENT","in_recovery":false,"writable":true,"source_fence_evidence":"trusted-runner-attestation"}}
```

`phase` 是 PG authority（AWS/COMMIT_INTENT/TENCENT），Redis authority 要读取 status.result.redis_authority。业务拒绝可能 exit0；必须解析 JSON code，不以 exit status 推断动作成败。解析/安装身份失败返回简短 `{"ok":false,"code":"..."}` 并 exit1，可能没有 op_id。EPOCH_CONFLICT 回复带当前 RPC epoch，调用者可用新的 op_id 查询；不能修改已有 op_id 的请求。

## 未知操作与持久边界

所有变更在外部效果之前 fsync 操作意图。全局 mutation.lock 排除不同变更同时执行；op lock 排除同 op 同时执行，ledger.lock 只覆盖短暂 checkpoint。PG authority 锁按 operation.lock → authority.lock 获取；不会持有 RPC ledger lock 等待子进程或网络。

超时/非零外部命令/失联返回 `UNKNOWN`，`result.reconcile_required=true`。UNKNOWN 的含义是“可能已经执行”。禁止把 UNKNOWN、EPOCH_CONFLICT 或 OP_ID_REUSED 当作源可以重新开写的证据。重放同一 op 或运行 guard，而不是改变 op_id 盲试。

PG arm/cancel 仅在 standby、sysid和源timeline匹配、replay达到最终LSN、只读且磁盘余量足够时接受。commit 在 arm 未过期时先 fsync RPC `promotion_intent`，再持久 authority manifest 的 `authority_mode=PROMOTION_INTENT`、同一 intent，最后执行固定 `pg_ctl ... promote -W`。接受之后 arm 过期不能取消。若在 ledger 与 manifest 之间崩溃，guard补全 manifest；若 promote 回复丢失，guard检查真实role并向前完成。primary必须 sysid相同、WAL timeline大于source、读写开启、current WAL LSN达到最终LSN。使用current_lsn允许primary重启后replay_lsn为NULL。guard即使原操作结果已完成也复核持久PG intent。

`source_fence_sha256` 和 `quiet_attestation_sha256` 是可信 AWS runner 的证据摘要。**目标 RPC 不能独立证明 AWS PostgreSQL 确实已停止。** root runner 必须先持久栅栏、证明源容器已停且不会自行重启，才提交该摘要。RPC成功返回明确标记 `trusted-runner-attestation`，不能将它写成目标独立验证的源停机证明。

应用 POST 先 fsync effect_started。若回复丢失，同 op只GET复核；满足目标后置条件则返回reconciled OK，否则保留UNKNOWN，不盲重发。Redis switch target 从已Config SHA锁定的app Env读取唯一 `MIGRATION_REDIS_TARGET_ADDR`（必须loopback host:port），复核实际address相同、paused/quiet、active_operations=0、tracked_sockets=0后才确认。source switch没有通用endpoint枚举证明时保留UNKNOWN，runner须检查实际status后决策。后台激活不盲重试。

Redis 预路由在 effect_started 前若新鲜 proof、status 或 epoch 检查失败，返回 UNKNOWN 并保留 INTENT；同一 immutable op 可重新检查所有前提，不能把一次临时检查失败缓存为不可恢复的终态。effect_started 后仍只允许观察既有后置条件，绝不再次 POST。预期 epoch 不得因失败而重写；实际 epoch 意外变化仍须保守调查。

Redis角色切换不在此程序内：可信源 runner使用源协调FAILOVER。RPC从不执行 REPLICAOF、SLAVEOF 或 FAILOVER。固定目标成为master后才保存COMMIT_INTENT/run_id，然后CONFIG REWRITE。使用owned volume中固定 `migration.conf`、O_NOFOLLOW、单hardlink/regular、文件uid与数据目录一致、模式600或644，rewrite前后uid/gid/mode不得变化；配置不得含有效replicaof/slaveof行。中途Refusal/超时均保持UNKNOWN向前恢复，绝不指示回源。Redis INFO只输出身份、offset与dbN汇总及其SHA256，不输出真实key。

## 本地验证

```text
python3 -m py_compile scripts/migration/target_rpc.py scripts/migration/target_rpc_test.py
python3 -m unittest discover -s scripts/migration -p target_rpc_test.py -q
```

测试只使用临时目录、fake backend和mock命令，不执行远端/网络/Docker。覆盖严格JSON、文件模式/symlink、程序路径/hash、epoch并发和op重放、LSN数值顺序、arm过期/取消、接受intent后丢回复/崩溃恢复、应用未知效果、持久端点顺序及禁止回退、Redis身份与rewrite。真实安装、Docker镜像/PG18/Redis行为、SSH强制命令、独立guard在断线/重启后的恢复及10秒累计等待需要联合演练后验收。

## 单窗口固定操作的持久子步骤

上述四项新动作均是封闭变更：没有调用者可指定的控制路径、epoch、endpoint、命令或凭据。每次只校验一次固定 app 容器并读取一次私控 token；所有实际控制仍经相同 loopback HTTP 和原 Go barrier，未绕过应用内保护。

每个有副作用的子步骤先采样组件 epoch，再将 control/endpoint/control_epoch/before/effect_started/INTENT 原子写入并 fsync，最后 POST。成功后 fresh GET 核验实际后置条件并保存 COMPLETE/body；ingress.resume 的 HTTP204 也通过后续 GET 核验。已经 effect_started 的子步骤重放只观察，不重发 POST；未达到后置条件保持 UNKNOWN。pg.route 的端点持久化有独立 persist 子步骤，保留 target→source 禁令。实例 ID 在首个控制前持久 pin；后续宏要求同一最近 quiesce 实例。

app.quiesce 的最终采样要求完整 15 项 writer registry、严格整数零计数、quiet/readiness/内嵌 ingress epoch 一致，且 PG/Redis 均已暂停；runtime.ready 在这时允许为 false。app.restore 先确认两项数据 authority 均已稳定为 AWS 或 TENCENT，且持久端点吻合，然后在同一个 mutation 锁中将既有 quiesce 标为 cancelled。任何旧 quiesce 的后续重放或 target guard tick 均不能继续 pause。恢复最终要求同实例 ready、ingress/writers/PG/Redis 都开放、缓存重建完成且实际路由与持久端点相符。若 ingress.resume 的回复丢失，已经恢复服务且出现新活动请求时仍可只观察完成；不会再次暂停。

在 effect_started 已 fsync、HTTP POST 尚未执行的间隙发生进程死亡时，仅凭后置状态可能无法确认效果；该子步骤会维持 UNKNOWN，而不是自动重发。quiesce 可由独立 app.restore 取消补偿；其他未知结果要求保留 ledger 并继续按既有主权边界处理。本次受控验收覆盖完整 quiet 之后主控制器挂起的独立恢复，不声称覆盖任意主机、网络或进程故障的十秒收敛。
