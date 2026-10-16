# AWS runner 固定本机适配层

`runner_backend.py` 接入 `durable_runner.DataHandover` 的 `save / boot_id / source / rpc / control / attest` 接口。模块导入和直接运行均不连接网络、不安装单元、不修改生产；只有已安装 root runner 实例化 `RunnerBackend` 并显式调用方法时才执行动作。

本模块不包括安装器、迁移调度入口、恢复 guard 或 DNS/入口管理。源端固定操作见 `SOURCE_OPERATIONS.md`，目标协议见 `target_rpc.py`。

## 固定配置与安装契约

安装器写 `/var/lib/sub2api-migration-20261016/runner/runner-config.json`，精确字段如下，未知字段拒绝：

```json
{
  "v": 1,
  "run": "20261016",
  "owner": "<32 lowercase hex>",
  "machine_id_sha256": "<64 lowercase hex>",
  "initial_target_epoch": 0,
  "ssh_key_sha256": "<SHA256 of exact root key file bytes>",
  "known_hosts_sha256": "<SHA256 of exact root known_hosts file bytes>"
}
```

`source-manifest.json` 的 owner 与 machine-id 摘要必须相同。engine checkpoint 也必须包含相同的 `v/run/owner`。所有私有文件 root 所有、0600、普通文件、单硬链接，scope 0700，拒绝 symlink 与可被普通用户替换的祖先路径。安装器负责安全导入既有 source-held SSH 私钥，只在 AWS 本机复制，不取回本地、不输出内容。

固定凭据与通道路径：

| 路径 | 内容 |
| --- | --- |
| `target-rpc.key` | 已审核的 source-held RPC 私钥，摘要固定于配置。 |
| `target-rpc.known_hosts` | 已核验目标主机密钥，摘要固定于配置。 |
| `rpc-control.sock` | 仅本后端使用的 root SSH ControlMaster socket。 |
| `state.json` | engine checkpoint。 |
| `transport-state.json` | 独立 transport、control 与 source 调用 ledger。 |
| `source-attestations.json` | source helper 使用的当前受信证明。 |
| `audit.jsonl` | root 私有追加审计：类型、时间、owner 和状态摘要，不含密钥或 DSN。 |
| `backend-state.lock` | 上述本机持久写入的短 flock。 |

目标固定为 `root@43.165.177.136:22`，远端命令固定为：

```text
/usr/bin/python3 -I /usr/local/libexec/sub2api-migration-20261016/target_rpc.py serve
```

实际权限由 root authorized_keys 中的固定 forced-command 授予；远端不执行 SSH 原始 command。SSH 不继承用户配置或 ssh-agent，使用 `-F /dev/null`、固定 key/known_hosts、StrictHostKeyChecking、IdentitiesOnly、无转发、无 PTY、无 ProxyCommand/ProxyJump/LocalCommand。请求 JSON 经 stdin 发送，秘密不进入 argv 或错误信息。

ControlMaster 只复用此固定目标的认证连接，ControlPersist=90s。`close_transport()` 通过固定 socket 执行 `ssh -O exit`，不停止业务数据隧道；调用者决定何时清理。调用前再次核实 key/known_hosts 摘要和 socket 所有者/类型/权限。不得把迁移数据 tunnel 的 socket 改成本路径。

模块与三个 sibling 库从同目录绝对路径加载，兼容 `python3 -I`。安装器应将它们放入 root 拥有且普通用户不可改写的固定 libexec 目录；root 主程序应复用本模块导出的 `engine` 或规范名 `durable_runner`，保持 `Unknown/Refusal` 类型一致。

## 状态与重放

`FileStore` 将 checkpoint 原子写入并 fsync 文件和父目录，再追加并 fsync 审计。锁只覆盖本机状态读改写和审计，不覆盖 SSH、源动作、HTTP 或睡眠。写入后审计失败仍属 `Unknown(STATE_PERSISTENCE_UNCERTAIN)`，不能解释成状态未保存。

transport ledger 的顶层精确字段是：

```json
{"v":1,"run":"20261016","owner":"<owner>","target_epoch":0,"operations":{},"controls":{},"sources":{}}
```

每个有 label 的 RPC op_id 由 `run + owner + namespace + label` 的 canonical JSON SHA256 前 32 位固定生成，source namespace 独立。完整 `request`、`expected_epoch`、请求摘要在发送前持久化。相同 label 重放只能使用原请求，不能根据新 epoch 重建。label 不同不能绕过 SourceOperations 的每动作唯一 ID 约束；engine 应始终使用 `source:<method>` 全生命周期 label。

每次 GET/status/redis.inspect 均使用新的随机 op_id，不复用旧结果。读请求发生 `EPOCH_CONFLICT` 时可以按响应中的新 epoch 用另一个新 ID 重探一次；变更请求遇到冲突则抛 `Refusal`，原请求保持不变。已完成变更也会向目标重放原请求，由目标 ledger 认领原结果，不能仅用本地缓存推断当前状态。

目标响应必须包含精确 envelope、相符的 run/owner/op_id/action、有限有效 epoch、字典 result 和一致的 ok/code。所有非 OK code 都被转换成 `Unknown` 或 `Refusal`；不完整/错误身份响应、网络超时、SSH 异常退出、非法 JSON 和未知操作结果都不能当成功。超时一律保留原请求，并抛 `Unknown`。

`transport-state.json` 不覆盖 `state.json`。源端自身的 action ledger 仍由 `SourceOperations` 管理，transport 只是记录调用身份与已收到的 phase。

## 方法契约

```python
backend = RunnerBackend()
# 离线测试可注入 config/store/transport/source/now；生产默认全部固定。
```

| 方法 | 行为 |
| --- | --- |
| `save(state)` | 验证 engine v/run/owner，root0600 checkpoint 与追加审计。 |
| `boot_id()` | 当前源主机 boot-id，供 engine 判断 monotonic deadline 是否仍有效。 |
| `source(label, method, **fields)` | 只接受固定 SourceOperations 方法；只读方法不传 op_id，也不缓存。变更使用稳定 op_id，`pending/unknown` 转为 engine.Unknown，complete 原样返回。唯一变更字段是 `redis_failover.timeout_ms`。 |
| `rpc(label, action, **fields)` | 使用封闭 target_rpc 协议和固定 SSH；返回 verified envelope 的 `result`。变更需要非空 label。 |
| `control(label, name, endpoint=None)` | 固定控制名；首次变更先 fresh GET 组件 epoch，持久保存 control.request，再发送 app.control。endpoint 仅 switch 接受 source/target。 |
| `attest(state, runtime, snapshot=None, sampled_started=None)` | 下述真实状态采样完成后，写 source helper 精确证明。 |
| `close_transport()` | 只退出专用 SSH ControlMaster，结果不影响数据主权。 |

control 原请求在超时后保持不变。若成功返回 HTTP204/空 body，后端使用新的 status RPC 获取 body；不会为 resume 生成另一个 mutation request 或更换 epoch。每次 `.status` 都 fresh，忽略传入 label 的缓存含义。

## 新鲜证明

`attest` 通过一次 fresh `app.quiet-snapshot` RPC 读取目标 ingress/writers/Redis/PostgreSQL status、当前 runtime 与 PG replay LSN，减少原来的五次跨云RPC。该动作只读、无自定义字段，每次生成新的op_id；目标端固定顺序采样、只校验一次app身份并复用同一个私控token，前后runtime实例一致、PG authority manifest与真实replay均重新读取。它保留原app/pg及组件body形状。源端继续对比传入 runtime 的 instance_id；包含全部网络耗时的完整采样超过 2 秒或 boot 改变则拒绝写入。quiet证明条件不变。

只有实际 ingress paused+quiet、writer held+quiet、当前 runtime serving 且未 draining、没有 active HTTP/WS、后台调度关闭、完整 15 项 Go writer registry 的所有任务 active/pending/failed/dropped 都为零，才可能声明业务 writer quiet。runtime 内嵌 ingress 必须同样 paused+quiet 且 epoch 与独立状态匹配，writer_coverage/quiet_controls 接线必须 ready。暂停期间对外 runtime.ready 及 PG/Redis readiness 本就为 false，因此不能用它们否定 quiet 证明。PG quiet 还要求 Redis paused+quiet+active_operations=0 和 PG paused+active_operations=0。传入 runtime 的“请求数为零”本身不能证明 writer 已封住。

写入形状与 source helper 相同：`v/run/owner/boot_id/checked_at/app_route_fenced/postgres/redis`。route_fenced 来自 engine 已持久验证的源入口状态；本模块不自行修改或推断入口配置。PG 字段包含本轮 final_lsn、target replay_lsn 和 arm_id；在首次 begin 尚未生成 final/arm 时使用 `0/0` 与全零 ID 哨兵，writer quiet 仍取实际状态。正式 PG stop 前，engine 必须完成真实 arm 后再次 attest。SourceOperations 仍要求证明年龄不超过 3 秒并自行检查最终源端会话和状态。

源 Redis 退役后重放 failover 可返回 `target_verification_required=true`；编排层必须紧随独立目标 RPC 检查当前 master、run_id 与既有 confirmed evidence 的身份一致，不能把旧 source ledger 当目标当前状态。

## 验证边界

```sh
python3 -m unittest scripts/migration/runner_backend_test.py scripts/migration/source_operations_test.py
python3 -m py_compile scripts/migration/runner_backend.py scripts/migration/runner_backend_test.py
```

backend 测试覆盖 intent-before-send、请求精确重放、跨进程稳定 ID、读请求新鲜度、epoch 冲突、204 resume、source pending/unknown、真实 Go paused schema 与完整 writer registry、writer/Redis 状态要求、状态持久化失败和固定 SSH 参数。source helper 另有独立测试。

这些均为离线可注入故障验证，不是生产迁移提交许可。root 文件权限、实际 forced-command 认证、ControlMaster 复用、systemd guard 独立存活、真实双端 PG/Redis 及累计 10 秒等待目标仍需安装前审查和隔离完整演练。目标 RPC 的 operation ledger 有容量限制；guard 不应无间隔高频轮询，也不能用重复旧 status op_id 掩盖容量问题。

## 生产单窗口与恢复路径

生产 Service.run 只调用 DataHandover.handover_all，一次 `kind=combined` 窗口连续完成 PG 与 Redis，取消旧有两个窗口间的五秒等待。内部总预算仍为 8 秒，用户上限仍为 10 秒，独立 guard 在内部预算余量0.5秒时接管（无先前暂停时为累计7.5秒），距用户上限仍留2.5秒。不可逆PG停库前要求内部预算至少剩5秒；不足则取消arm并恢复AWS。开始前 fresh runtime 和源 resource_fences 检查位于窗口外；源应用必须已干净退出、restart=no 且入口已封住。

无故障窗口内固定 11 次目标跨云 RPC（真实 RunnerBackend 适配器测试统计，包含原本控制层隐藏的 GET/204 行为）：

1. app.quiesce：返回完整 quiet snapshot，源端直接复用该刚返回的快照写入初始 attestation。
2. pg.arm。
3. app.quiet-snapshot：带新 arm 的 source attestation。
4. pg.commit：之前已 fsync 本机 COMMIT_INTENT，并由 SourceOperations 完成独立 pg_stop 栅栏；正常路径直接提交原 arm，目标 commit 自行严格核验 authority/arm/fence。仅恢复路径重新查询 status，必要时 cancel/rearm。
5. pg.route：目标本地持久 PG 端点并切换暂停的 SQL 池。
6. redis.preroute：之前已 fsync 本机 Redis COMMIT_INTENT；目标完成完整 quiet/replication/identity 证明并关闭旧池。
7. app.quiet-snapshot：为源协调 FAILOVER 写入 fresh 证明。
8. redis.inspect：源 FAILOVER 完成后独立核对 source slave、target master、run_id、无 failover。
9. redis.persist-primary：返回 persisted=true、authority=TENCENT 和相同 run_id。
10. app.persist-endpoints：PG/Redis 都已确认后持久双 target，checkpoint 保存 redis_target_run_id。
11. app.restore：按固定顺序重建缓存、恢复消费和请求。

旧源 Redis 已被协调 FAILOVER 降为 replica、restart=no，源应用已停，后台仍关闭，唯一写主是已确认且持久化的目标。源 Redis 退役可在请求恢复后进行：Service.run 在正常 handover_all 或 guard 恢复后、最终三源退休检查之前调用 retire_source_redis。它使用相同 SourceOperations 生命周期 ID 完成旧 replica 的干净停止，并再次 fresh redis.inspect 核验目标仍是同一 run_id 的 master。只有这些完成后才激活后台、进入 COMPLETE；未知退休结果不会重新暂停已恢复的业务，也不能绕过最终退休验收。

复用快照时 `sampled_started` 是该 RPC 发送前的源时间；源端仍检查包含网络在内的 2 秒新鲜度，以及 checkpoint 的 boot_id。其余 attestation 每次 fresh 读取一次 app.quiet-snapshot。缓存的 quiet_instance_id 仅用于比较实例身份；所有 quiet 计数和状态来自 fresh 快照。

recover 首先读取目标完整 status 和源固定 resource_fences（只检查三项已 pin 容器身份/运行状态，不做 Redis 全量角色检查），禁止因本机 checkpoint 落后而回源。尚未不可逆的 combined 窗口只需 status + resource_fences + app.restore；如有 arm 先固定取消。PG 已停或提交已接受后只向前完成两项数据。生产 selftest 使用同一 combined quiesce/restore，quiet 完成后故意挂起主控制器，由独立 guard 杀空主 cgroup 再恢复。旧的逐组件和分窗接口仅供兼容，生产入口不再调用。

11 次统计证明协议回合数量，离线时钟不证明真实耗时。必须由 root 安装、核验哈希，并实测受控恢复和实际端到端迁移的累计排队时间后才判断满足时间预算。effect_started 之前/之后的未知结果边界与 TARGET_RPC.md 相同。
