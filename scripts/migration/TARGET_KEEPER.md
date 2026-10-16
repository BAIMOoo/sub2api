# 腾讯迁移完成后的持久运行

`target_keeper.py` 只管理本次迁移的三个固定原容器和既有
`sub2api-tencent-entry.service`。默认完全离线；只有安装后的 root
`/usr/bin/python3 -I /usr/local/libexec/sub2api-migration-20261016/target_keeper.py run`
具有启动路径。本文和本地测试没有安装、enable、启动、停止或修改远端资源。

Docker `restart=no`、容器 ID、Config SHA、镜像和卷保持原值。keeper 发现运行中的实例时只接管观察；运行不健康时报告问题，不发送 restart/stop/信号。程序没有 pull/create/recreate、PG promote、Redis REPLICAOF、回 AWS、修改端点、修改 restart policy 的入口。停止 keeper 只停止其 Python 进程，systemd unit 没有 ExecStop/ExecStopPost，使用 `KillMode=process`。

## 安装与可信 arm 的责任边界

领导者负责审核后安装 root0700 程序和 root0644 systemd unit；unit 精确内容由 `target_keeper.unit()` 返回。它以 root 运行、`After/Requires=docker.service`，自身异常退出可重启。root0700 scope 固定为：

```text
/var/lib/sub2api-migration-20261016/keeper/
  arm.json       root0600，领导者在迁移完成后安全传输、atomic replace+fsync
  state.json     root0600，keeper 独立 journal，atomic replace+fsync
  keeper.lock    root0600，单进程 flock
```

所有祖先不可 symlink、group/other 不可写；私有文件必须 regular、单 hardlink。RPC 程序在执行 import 前也验证 root0700 文件和可信祖先；随后复用其 `load_config` 验证安装位置、程序 SHA、machine-id、closed schema。keeper 自身 SHA 由 arm 固定。

应用挂载目录按实际准备规格分开核验：`app/` 和其祖先必须 root；只有 `app/data/` 叶目录为 uid1000、0700。PG/Redis volume 的父目录保持 root 信任检查，卷内数据由容器数据库用户所有（本次实机 uid999）可正常通过；不对卷内目录错误套用 root-only 检查。Redis 配置继续复用 RPC 的“数据目录为非 root 用户、配置 owner 与目录一致”检查，不放宽控制程序、端点或私有 ledger 的 root 所有权。

**只有领导者可以生成 arm。** 它必须先在 AWS root runner 完成一次新鲜 `verify_complete`，读到 `phase=COMPLETE / pg=redis=TENCENT / window=null`，核实源 app/PG/Redis 三者都 exited、exit0、无 OOM、restart=no，以及对应持久 retirement/tombstone/部署路径栅栏证据。应保存原始 root 证据包，再通过已固定主机身份的受限 SSH 传到腾讯 root scope；不得由普通用户家目录的文本生成“已完成”证明。

同次 arm 还必须在腾讯核实：持久 target/target、RPC 两个 TENCENT authority、PG manifest PRIMARY 与实际同集群可写 primary/前进 timeline/最终 LSN、Redis master 和成功 CONFIG REWRITE 记录、应用实际 target/target、后台 activation 完整成功、入口健康。arm 是领导者对这些证据的可信声明，keeper 本机无法独立证明 AWS 已停机。其后恢复只依赖腾讯本地持久证据，AWS 离线不会阻塞恢复。

arm closed schema 如下。占位符须替换，所有 `*_sha256` 为64位小写 hex，`redis_persist_op_id` 为32位 hex。摘要使用 targetRPC `sha` 的 canonical JSON 规则；程序摘要是原始文件 bytes SHA256。

```json
{
  "v": 1,
  "run": "20261016",
  "owner": "<RPC owner>",
  "machine_id_sha256": "<腾讯 machine-id SHA>",
  "rpc_config_sha256": "<实际 RPC config 对象 canonical SHA>",
  "program_sha256": "<安装的 target_keeper.py bytes SHA>",
  "armed_at": 1792166401,
  "complete": {
    "phase": "COMPLETE",
    "pg": "TENCENT",
    "redis": "TENCENT",
    "window": null,
    "verified_at": 1792166400,
    "state_sha256": "<AWS root runner 最终 state SHA>",
    "verification_sha256": "<AWS fresh verify_complete 结果 SHA>",
    "source_machine_id_sha256": "<AWS machine-id SHA>",
    "source_inspect_sha256": "<AWS fresh source inspect 完整结果 SHA>",
    "source_tombstones_sha256": "<三个源资源 retirement/tombstone 证据包 SHA>",
    "source_resources": {
      "app": {"id":"<64hex>","running":false,"status":"exited","restart":"no","exit_code":0,"oom_killed":false},
      "postgres": {"id":"<64hex>","running":false,"status":"exited","restart":"no","exit_code":0,"oom_killed":false},
      "redis": {"id":"<64hex>","running":false,"status":"exited","restart":"no","exit_code":0,"oom_killed":false}
    }
  },
  "pg_intent_sha256": "<RPC/PG manifest 共同 promotion_intent canonical SHA>",
  "redis_persist_op_id": "<RPC 已完成 redis.persist-primary operation ID>",
  "redis_normalized_config_sha256": "<normalized_redis(实际 migration.conf bytes, targetRPC.sha)>"
}
```

`armed_at - verified_at` 必须在0至30秒之间；领导者负责取得真实新鲜证据，超时重验。接受后的 arm 不因时间流逝过期，才能用于以后开机。keeper journal 绑定完整 arm SHA；替换 arm 时旧 journal 会拒绝，禁止为了绕过 UNKNOWN 意图直接删改 journal。

只有新 keeper 需要 enable。既有 Caddy entry 保持 disabled，由 keeper 在依赖准备好后 start；已经 active 的 entry 只观察。否则 entry 自身的 `After=network/docker` 会在冷启动时抢先开放端口。这里不改既有 entry unit 的字节或 hash，也不 reload 正在服务的 Caddy。

## 一次恢复迭代

1. 验证 arm/自身 SHA/配置绑定，读取独立 journal。再读持久 target/target、RPC TENCENT authority、最终 PG intent、成功 Redis persistence operation；有未完成迁移 mutation 时拒绝。
2. inspect 固定 PG 原容器。运行中只观察。确切 exited 时，先核原 image/Config/volume/labels/network/restart=no、PG_VERSION18、verified seed、pg_control 文件存在且无 symlink、无 standby.signal/recovery.signal，保存 start INTENT 并 fsync，再 start **原 ID**。随后 fresh SQL 核实 sysid、版本、可写 primary、timeline 和最终 LSN，未通过不启动下游。这里不在宿主解析 PG 控制文件的二进制 sysid；在线 SQL 是实际集群身份的验收点。
3. Redis 同样只在原容器 exited 时 start；启动前后都验证可信 migration.conf 无 include/replicaof/slaveof、标准化 SHA 相同，fresh INFO 必须 master/no-failover。run_id 随进程重启变化，不把旧 run_id 当作永久身份，也不重新执行 migration 的 redis.persist-primary。
4. app 仅在两主端已验证后 start 原 ID，随后要求 serving/ready、非 draining、PG endpoint target、Redis127.0.0.1:26379/cache ready、各控制未暂停/held。原环境的 `MIGRATION_BACKGROUND_WORKERS=false` 保留。
5. 后台按 `(container ID, boot ID, Docker StartedAt, PID, runtime instance_id)` 生成新代际。已完整激活则 adopt；否则再次核实主权/同一进程，保存 activation INTENT 并 fsync，最多一次 POST `background.activate`。丢回复只 GET：activated=true、activating=false、pending空、无 failures、runtime enabled且同 instance 才完成。保持 UNKNOWN 或部分失败时不会再次 POST，不重启应用。
6. 最后 inspect/start 既有 entry unit。核实固定 unit/Caddy/config SHA、FragmentPath、无 drop-in/job，active/running/MainPID>0 即 adopt；确切 inactive/failed且 PID0 才保存 intent 后 start 一次。Caddy 证书允许正常续期，不固定旧 leaf SHA。

三个容器每个 arm 最多3次 start 尝试，防止永久 crash loop；一次未知 start 不盲重发。entry 每个 boot 最多一次 keeper start，累计也最多3次；entry 自身原有 Restart=on-failure 继续负责其故障恢复。达到限制需要领导者调查，不能自动清 journal。每2秒只读采样；状态不变时不重复日志。不调用 `TargetRPC.handle`，不消耗其4096条 operation 上限，不修改其 epoch/ledger。

Redis 原启动脚本会删除 CONFIG REWRITE 自动生成的无参数默认 loadmodule 行，避免官方 entrypoint 重复加载。keeper 标准化只忽略这些审核目录下的无参数 `.so` 行及注释/空白；其他配置变化会拒绝，不执行 CONFIG REWRITE。

## 验证及当前边界

```sh
python3 -m unittest discover -s scripts/migration -p target_keeper_test.py -q
python3 -m py_compile scripts/migration/target_keeper.py scripts/migration/target_keeper_test.py
python3 scripts/migration/target_keeper.py
```

离线故障测试覆盖：冷启动顺序、adopt无服务动作、不健康不重启、缺少完成证明/源未退役/主权回退拒绝、角色信号拒绝、原容器/卷身份、start/activation fsync失败、丢回复与不重复、跨进程后台恢复、部分启动失败、crash-loop上限、真实原子私有文件权限和arm变更拒绝、无 stop hook。

尚未远程安装或开机演练。真实冷启动验收应在独立克隆环境完成 PG→Redis→app→后台→entry 全链；当前生产接管只核实 adopt、PID/连接不变以及后续只读健康，不能为验收主动重启生产。

单腾讯主机整机宕机会影响可用性，这个守护不能提供整机零停机。已有 entry 在 app 异常退出时继续运行，恢复 app 到后台激活之间可能已有请求进入；Go ready 当前不包含后台激活条件，本实现不新增在线入口暂停。Redis 保留源 RDB/appendonly=no 策略，断电时未快照 Redis 写入的持久性没有因此提高；不把“自动恢复进程”表述成“断电零丢失”。
