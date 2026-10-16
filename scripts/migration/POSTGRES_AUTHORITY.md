# PostgreSQL 主备兼容启动规格交接

`prepare_postgres_authority.py` 只把已准备好的腾讯 PostgreSQL 副本交给可识别主备角色的新启动规格和监控。默认执行完全离线；`--inspect` 只读两端；`--apply` 仅改变腾讯本次拥有的副本。它没有提升、修改源 PostgreSQL、切换应用端点、业务写入或 DNS 操作。

```sh
python3 scripts/migration/prepare_postgres_authority.py
python3 scripts/migration/prepare_postgres_authority.py --inspect
# 根代理审查并通过准备条件后，才使用以下显式执行入口：
python3 scripts/migration/prepare_postgres_authority.py --apply
```

截至本 slice 交付，只执行过离线测试、隔离本地 Docker 测试及真实 `--inspect`，未对 AWS/腾讯执行本脚本 `--apply`。

## 为什么需要这一步

原 `prepare_postgres_replica.py` 的容器命令固定 `default_transaction_read_only=on`，每次启动还要求 `standby.signal`。直接提升后默认仍只读，重启还会因 signal 消失而退出。原 target monitor 会把任何新主当作违规副本并停止它。

新启动脚本只接受已经验证的 PG18 数据目录，不初始化、不重新 basebackup、不删除数据。它设置 `default_transaction_read_only=off`；处于恢复中的 PostgreSQL 仍强制事务只读。未来经过独立授权的提升结束后，同一规格可以写入并作为主库重启。脚本本身不具备提升入口。

PG18 官方镜像声明 `/var/lib/postgresql` 父 VOLUME，即使明确挂载 child PGDATA，也会自动生成额外匿名父卷。本工具把旧父挂载描述符存入清单且保留在停止的旧容器上，不采用或删除该匿名卷。新容器显式用 1 MiB tmpfs 覆盖空父目录，避免创建新的匿名卷；真实数据仍是原 named child volume。

## 预检和固定资源

源、目标沿用现有 `prepare_channel.py` 的受限 SSH argv。远端执行 root 读取/检查，原准备文件作为本地审核过的 library 注入独立非 main namespace，不从远端普通用户可写目录加载代码。

每次 apply 都取得新鲜源只读证明：主库 system identifier/timeline、既有 owner、独立 source monitor、有限 WAL 保留上限、源磁盘及 owned slot。第一次交接要求源 slot active、目标在流复制中、旧 target monitor 活跃且最近 sample 健康。中断重试允许 slot 暂时 inactive，因为目标可能已经正常停止；slot 失效/丢失、源身份变化或 guard 不健康仍拒绝。

目标核实相同 owner、Config 摘要和有序 RootFS DiffID、PG18.3、相同 source timeline、固定 `primary_slot_name`、sender `127.0.0.1:15432`、业务端口 `127.0.0.1:25432`、实际 read-only、verified seed marker 和 PG_VERSION18。替换后还要求 replay LSN 至少达到本次源预检的 flush LSN；这是规格交接证明，不是最终业务 RPO 或 promotion 许可。

| 资源 | 固定名称 |
| --- | --- |
| 当前副本容器 | `sub2api-migration-pg-replica-20261016` |
| 停止保留的旧规格容器 | `sub2api-migration-pg-preauthority-20261016` |
| 原数据卷 | `sub2api-migration-pg-replica-data-20261016` |
| 新 root scope | `/var/lib/sub2api-migration-20261016/postgres-authority` |
| 新 monitor/timer | `sub2api-pg-authority-20261016-monitor.service` / `.timer` |
| 原 target monitor/timer | `sub2api-pg-replica-20261016-target-monitor.service` / `.timer` |

目录 0700，状态/pgpass/错误日志 0600，生成 monitor 程序 0700。可信 scope 的所有祖先必须 root-owned、非符号链接且不可被 group/other 写入。旧 pgpass 只在腾讯本机复制到 root scope；不输出、不回传、不进入 argv。保留数据卷、原停止容器和原 monitor unit 内容备份，不执行 volume rm 或递归清理。

## 串行交接顺序

1. 写入 root `state.json` adoption intent，固定旧容器 ID、原卷路径、旧父挂载、owner、镜像和复制身份。安装新 role-aware monitor/timer并取得一次真实健康 sample。
2. 只 disable/stop 旧 timer。等待其已经在执行的 oneshot 自然结束，检查 MainPID0、无 pending Job；不杀 monitor 或其 docker 子进程。等待超过35秒拒绝后续替换。
3. 原 unit 内容须逐字匹配审核版本。保存 0600 备份，原子替换成持久 `/dev/null` mask、fsync 父目录并 daemon-reload。再次确认 masked、idle、MainPID0。重试识别已安装的自有 mask；不恢复旧 monitor。
4. 再次核实当前 container 真正仍在 recovery、身份正确且无 promotion intent，发送 SIGINT 请求 PostgreSQL 正常 fast shutdown。无活动业务连接，因为该副本尚未被业务接入。等待干净退出，不使用 SIGKILL、`docker stop` 的隐式强杀或删除容器。
5. 原容器 rename 为固定 archive，保持停止且 restart=no。新容器使用同一 data volume、固定镜像、loopback listener和原资源限制；启动模式兼容主备，restart policy 仍为 no。
6. 等待复制恢复、replay达到源预检LSN、事务仍只读而默认 readonly=off；保存新 container ID，状态进入 `STANDBY / standby-role-aware-ready`，确认新 timer/sample健康。

原 **source monitor、source timer和source独立OnFailure fence全部保留**。其后续退役属于最终源主停机的受控交接，本工具不接管。正式使用前还需要在腾讯确认新规格一次真实 standby 重启后恢复复制；本工具 `--apply` 本身只正常重启为新规格一次，不提供再次重启或 production 主库操作入口。

## 未知执行结果与重试

`--apply` 每次重新只读检查源，目标持有 `operation.lock`。外部命令前保存 intent；SSH中断或 timeout 时不回退、不删除资源。再次 inspect 后可重试同一 `--apply`：

- 旧 monitor已 mask但checkpoint响应丢失：核对确切备份、mask及实际idle状态后继续。
- rename响应丢失：通过固定 original ID 找到停止archive；不重复采用新对象。
- docker create响应丢失：新容器必须符合 owner/run/authority标签、固定镜像/命令/挂载/资源才能补录ID；不覆盖冲突对象。
- start响应丢失：检查实际运行态并恢复只读复制验证；不重复初始化或换卷。
- 容器已经被提升、signal已消失、出现promotion intent或角色不明确：拒绝 apply。不能通过该准备工具恢复旧主或清理新主。

原 prepare 的 target status/cleanup此后不能管理新阶段：其命令和只读判断针对旧规格，而且 cleanup可能删除数据卷。旧 unit被mask后，重跑原prepare不能重新启用旧 monitor。不要为通过原 status而 unmask。

## 监控角色约定

新 monitor始终先取得 owned volume磁盘情况，再检查固定容器/集群。磁盘低于2 GiB时，仅在 `ADOPTING` 或 `STANDBY`、没有任何 promotion intent、实际 recovery=true 且身份正确时，重新取得 `authority.lock` 并再次读取角色，然后正常停止 owned standby。源 WAL guard继续保护源。诊断失败、身份不符、角色未知、意外primary或已授权primary都只写不健康/磁盘告警，**不自动停止可能已经服务生产的新主**。

SQL不可用时仍保存已经取得的磁盘余量；不把查询失败解释为“肯定是可停止的副本”。如果 Docker、文件系统、systemd或SQL持续不可用，不能保证及时停止，监控会不健康；它不能代替主机级磁盘监控。

所有未来角色转换必须先获取同scope的 `operation.lock`（与准备/重试互斥），再获取 `authority.lock`（与standby磁盘stop互斥）。在最终源fence和LSN证明成立后，未来RPC先持久保存自身操作ledger，再原子更新以下字段并fsync文件与父目录：

```json
{
  "authority_mode": "PROMOTION_INTENT",
  "promotion_intent": {
    "op_id": "<32个小写hex字符>",
    "source_fence_sha256": "<64个小写hex字符>",
    "final_lsn": "8/CA000000",
    "system_identifier": "<清单中相同的system identifier>",
    "source_timeline": 1,
    "accepted_at": 1792166400.0
  }
}
```

本工具不设置此intent，也不执行角色改变。有效intent只表明未来独立RPC接受了源fence证明，monitor并不会从摘要本身证明源已停机。`PROMOTION_INTENT`模式允许仍处recovery的过渡，也接受同system ID、timeline前进且可写的主端；source已停导致receiver消失时暂不健康但绝不stop。未来RPC核实提升完成后设置 `PRIMARY` 并保留同intent。主端timeline使用当前WAL文件名前8位，避免仅看尚未更新的checkpoint timeline。

原target monitor完全移交后才允许提升。新monitor对未授权primary也不会stop，必须由上层保持入口关闭并处理主权异常；它不自行反向恢复AWS。

## 本地验证与证据边界

```sh
python3 -m unittest discover -s scripts/migration -p 'prepare_postgres_authority_test.py' -q
SUB2API_PG_AUTHORITY_DOCKER_IMAGE=postgres:18-alpine \
  python3 -m unittest discover -s scripts/migration -p 'prepare_postgres_authority_test.py' -q
python3 -m py_compile scripts/migration/prepare_postgres_authority.py scripts/migration/prepare_postgres_authority_test.py
```

Docker选项只使用已经缓存的本地镜像，不pull。全部容器 `network=none`、无published port，通过独立Unix socket卷复制；名称随机、带测试owner标签。验证真实 PG18 basebackup后的新bootstrap、standby重启恢复复制，以及**仅测试内**提升后可写、持久化数据并再次重启仍为主端。finally按本次标签清理容器和卷。测试里的本地提升不构成迁移工具可调用能力。

离线测试覆盖生成Python编译与sh语法、默认/inspect无变更、同卷与复制身份、主库/未知角色不stop、旧oneshot等待及持久mask、rename/create/start成功但响应丢失的重试、源保护失效、已交出主权拒绝apply。真实只读inspect可验证当前两端兼容条件，但不证明远端mask、systemd执行、真实amd64规格切换重启或最终promotion/RPO；这些必须由根代理审查后另行执行验证。
