# Sub2API 脱离当前会话的迁移控制器设计

> 实施状态更新（2026-10-17）：当前实现采用累计8秒内部预算、10秒用户上限的单窗口 PG+Redis 交接；正常窗口内11次跨云 RPC，主控 Restart=no，由独立 guard 恢复。下文保留早期设计推导，涉及双窗口、Restart=on-failure 或拟新增接口的段落已被实现取代。实时主权、演练及切流状态以 SUB2API_MIGRATION_LIVE_20261016.md 为准。

状态：设计待根代理审查，2026-10-16。本文件不安装服务、不修改两端主机、不切流、不提升数据副本。准备脚本的 `standby_ready` 也不构成本设计的切换许可。

## 1. 目标与现有边界

AWS `13.231.143.136` 的独立 systemd runner 执行全流程。腾讯 `43.165.177.136` 的固定动作 RPC 执行目标端操作。安装后，即使本地电脑、SSH 会话、当前 Codex 对话和模型请求全部停止，控制器仍能推进、在可回退阶段中止、写下可恢复状态。

迁移顺序是：API 到腾讯但继续使用 AWS PostgreSQL/Redis；旧 AWS 应用自然排空并正常退出；单独窗口交接 PostgreSQL；恢复 API；另一个窗口协调交接 Redis；恢复 API；最后启用腾讯后台任务。旧 AWS 数据库与缓存不能再次成为写入主端。DNS、入口桥、数据主权分别记录，不能用 DNS 回退替代数据恢复。

用户接受的是**同一个请求因迁移累计增加的等待最多10秒**，不能把两个窗口解释成各自可消耗10秒。已经开始的 HTTP、SSE、WebSocket 保持原连接直至自然结束。等待长连接结束发生在窗口之外；窗口内也不能用断开连接来制造 quiet。冻结后台配置修改、账号导入、充值兑换等操作持续到完整验收结束。

10 秒是进入正式切换的验收门槛。健康路径必须经完整演练给出实测证据；提交前恢复路径必须留足时间。任一提升动作已经被目标端持久接受后，遇到主机、数据库或网络故障只能向新主恢复。此时无法同时承诺任意故障下的 10 秒和零错误，控制器必须记录 `deadline_breached`，保持单写主端，不能宣称迁移满足目标。尚无端到端演练证据时禁止进入生产提交阶段。

## 2. 代码与服务部署面

拟新增的实现文件：

- `scripts/migration/durable_runner.py`：安装器、AWS runner、AWS recovery guard；默认离线计划，安装与启动显式区分。
- `scripts/migration/target_rpc.py`：目标 root forced-command RPC 与本地恢复服务，共用固定操作实现。
- 对应离线测试、可注入故障的独立容器演练。不得在单元测试中访问 AWS、腾讯或 Cloudflare。

AWS 的受信程序安装在 root-owned `/usr/local/libexec/sub2api-migration-20261016/`；状态在 `/var/lib/sub2api-migration-20261016/runner/`。腾讯受信程序使用相同 `/usr/local/libexec/` 前缀；RPC 状态在 `/var/lib/sub2api-migration-20261016/rpc/`。目录 0700，状态、凭据和私有日志 0600，程序 0700。拒绝 symlink、非普通文件、意外所有者及可被普通用户替换的路径祖先。

不要让 root 执行 `/home/ubuntu` 下可由 ubuntu 替换的脚本。现有准备目录和 owner manifest 仅用于受检导入：导入时核实随机 owner、不可变容器 ID、镜像 Config 摘要、有序 RootFS DiffID、卷标签、PG system identifier、Redis run ID/复制 ID，然后写入 root 私有清单。每个动作再次核实实际对象。

拟安装服务：

| 服务 | 作用与停止约束 |
| --- | --- |
| `sub2api-migration-runner-20261016.service` | AWS 主控制器，`Restart=on-failure`，无成功前自动卸载；有阶段目标和截止时间，不依赖 stdin/TTY。 |
| `sub2api-migration-recovery-20261016.service` | AWS 独立 deadline/runner 存活检查，短周期单独进程；只按主权记录中止或恢复。主 runner 的 OnFailure 触发同一恢复逻辑。 |
| `sub2api-migration-target-guard-20261016.service` | 腾讯本地观察已接受的操作，RPC 连接关闭后继续完成或取证，不依赖 AWS 收到成功响应。 |
| 原有 channel service | 保持 SSH 转发；准备阶段一次性扩展所有所需端口，禁止在 basebackup、quiet 或生产流式请求期间重启隧道。 |

systemd 的停止钩子只能调用主权感知的恢复逻辑，不能无条件打开入口、启动旧数据服务或停止腾讯新主。外部命令有独立 timeout；超时代表结果待核实，不代表动作未发生。状态短锁不能跨持有网络调用，避免恢复 guard 被主进程卡住。

## 3. 凭据和受限 RPC

AWS 已有迁移 SSH 私钥继续只留在 AWS。安装器可在 AWS 本机将其复制到 root 私有 scope 并固定公钥摘要；不得下载到本地或输出。固定 known_hosts，严格主机校验，禁用 ssh-agent 及用户 SSH 配置继承。

腾讯为该公钥添加 **root 账号专用** authorized_keys 条目：精确 `from="13.231.143.136"`、`restrict`、固定 `command="/usr/local/libexec/sub2api-migration-20261016/target_rpc.py"`。该条目不开放任何转发、PTY、agent 或交互 shell。现有 ubuntu 隧道条目及其受限转发保持独立，不能用重复的同账号公钥行猜测 sshd 的匹配顺序。先检查 root 公钥登录策略；若禁止，不修改全局 sshd 策略绕过，应单独评审专用账号和固定 sudo 包装器。

RPC 原始 SSH command 不参与执行；只读最多 16 KiB 的一条 stdin JSON，拒绝未知字段、重复键、尾随内容、非 UTF-8、非有限数、错误 run/owner/epoch 和越界长度。响应只包含固定错误码、角色、阶段、身份摘要和数值。外部命令始终使用固定 argv，禁止 shell、任意路径、容器名、DSN、SQL、Redis 命令、URL、systemd unit 名称或环境变量输入。

请求形状：

```json
{"v":1,"run":"20261016","op_id":"<固定长度随机标识>","expected_epoch":17,"action":"pg.commit","arm_id":"<已签发标识>"}
```

`action` 是下面的封闭集合；名称为设计接口，代码审查可以收敛，但不能放宽参数面：

| 动作 | 固定效果与必须核实的条件 |
| --- | --- |
| `status` | 只读 root 清单和实际资源；返回当前 app/PG/Redis/monitor 主权及最近操作结果。 |
| `app.start-source-data` | 只启动经过审查的 owned 腾讯应用固定规格，数据端点已私下配置为 AWS 通道；后台调度关闭，禁止接收任意镜像/env。 |
| `app.control` | 枚举子动作映射到固定本机控制地址和路径，只接受预期 epoch 与 `source/target` 枚举。包含入口、writer、PG、Redis控制。 |
| `entry.start` | 安装并启动已审核的 API 专用 Caddy unit；固定域名/证书/8082 upstream，不涉及 Codex 网页服务。 |
| `pg.adopt-promotion-ready` | 在窗口外交接 standby monitor，给同一 owned 卷安装可重启的主/备兼容容器规格；仍验证 recovery/read-only/复制。 |
| `pg.arm` / `pg.cancel-arm` | CAS 创建/取消短期提升许可，记录 source 身份/已排空证明摘要/最终 LSN；取消成功后禁止迟到的 commit。 |
| `pg.commit` | 许可有效、源停机 fence 已核实且目标已回放最终 LSN时，先持久化 commit intent，再提升固定集群，确认可写。 |
| `pg.reconcile` | 按 recovery、timeline、system ID 和已存 intent 核实未知结果；不得重新 basebackup 或重置卷。 |
| `redis.arm` / `redis.cancel-arm` | 核实固定 standby、反向链路、offset、全局 quiet 与角色监控，记录协调交接许可。 |
| `redis.reconcile` | 读取双端角色转换证据；目标已成为 master 后记录不可回退主权，完成 target CONFIG REWRITE。提升由源端协调 FAILOVER 发起，不接收任意 `REPLICAOF`/`FORCE`。 |
| `authority.verify` | 验证两个业务数据端点都为腾讯主端，清单与应用实际 epoch 一致，旧端保持 fence。 |
| `background.activate` | 两数据端已交接、API已恢复后，显式激活目标后台任务并核实组件清单。 |

RPC root ledger 在每次有副作用前写入 `op_id + intent + expected_epoch` 并 fsync 文件/父目录。相同 op_id 相同请求重放返回相同结果或现场 reconcile；相同 op_id 不同内容拒绝。对已完成的旧 epoch 操作只能查结果，不能重复执行。不能仅以网络异常作为 `pg.commit`/FAILOVER 失败依据。

Cloudflare token 由本地安装器经 SSH stdin 发送给 AWS 私有安装路径，原子写入 root 0600 文件。runner 从该文件读取，禁用 shell tracing，token 不进 argv、systemd Environment、journal 或响应。只允许固定 zone/record 的 `api.b4im.com` A 记录读取和内容更新，保持 `proxied=false`、TTL60 和其他字段；每次写前核实完整记录身份和当前地址，成功后读回。未知地址、多个冲突记录、外部改动均停止 DNS 分支。DNS 请求在 quiet 窗口之外，不能占用10秒预算。

## 4. 持久状态与恢复原则

源、目标分别保存 append-only 事件与原子 checkpoint，至少包括：run/owner、程序摘要、两端 machine-id 摘要、资源 IDs、镜像身份、当前阶段/epoch、每个 RPC 的 intent/result、route、DNS读回、应用 instance ID、ingress/writer/PG/Redis epochs、PG最终LSN/timeline、Redis复制ID/offset、两数据主权、monitors模式、窗口开始/截止/最老排队时间和失败证据。

单机 flock 和 epoch CAS 防止 runner、recovery guard、手动恢复并发推进。重启后先 reconcile 真实角色和未决操作，再决定动作；不能直接从上次的“即将提升”继续。主机 boot-id 改变后，旧 monotonic deadline 失效，按持久 UTC截止进入恢复，禁止重新给窗口10秒。正常运行用 monotonic 计时，UTC只用于跨重启及审计。

主权分别是 `AWS`、`COMMIT_INTENT`、`TENCENT`、`UNKNOWN`。目标端已持久接受 PG提升 intent 或 Redis 已开始不可确定的角色切换后，源端不再擅自恢复旧写主。只有目标端 CAS 取消未提交许可并确认没有已接受的提升、没有迟到动作，才允许走提交前回退。任一数据端已提升后，该数据端不得回旧数据；另一个尚未交接的数据端仍可保持其当前主端。

`safe_to_stop` 当前故意恒为 false，不能用它作为成功或失败判断。`BackgroundTaskCoverage=partial`、某个队列长度0或 `/ready` 成功也不能替代明确的 producer fences。写入静默需要完整组件清单、全部 active/pending0、旧应用已退出，以及外部写入者清单为空。

## 5. 阶段状态机

| 持久阶段 | 成功条件 | 失败后的动作 |
| --- | --- | --- |
| `PREPARED` | 固定镜像/卷/身份/通道、PG/Redis副本、RPC、凭据和独立guard都验证；没有改业务路由。 | 继续AWS；保留有界副本和监控，不删除数据。 |
| `TARGET_SOURCE_DATA_READY` | 腾讯应用使用AWS PG/Redis，quiet控制初始化，后台未激活；健康/鉴权/所需本地文件检查通过。 | 停用未承载流量的新应用必须先排空；AWS继续服务。 |
| `API_BRIDGED` | 精确入口规则让新TCP走腾讯；旧AWS连接保持；目标Caddy直接入口可用；规则回读与身份检测通过。 | 两端仍用AWS数据，停止新流量进入腾讯后自然排空，撤销精确规则；禁止停止任何已有连接。 |
| `DNS_TARGET` | 固定A记录已读回腾讯；AWS桥继续服务旧DNS/旧客户端连接。 | 可恢复A记录为AWS，但AWS桥和腾讯既有连接保留；数据仍AWS。 |
| `SOURCE_APP_DRAINING` | 源应用所有接入连接自然消失；不能通过强制断连或重载生产Caddy制造0。 | 保持本阶段独立等待；不存在最长流时长保证，不能强制完成。 |
| `SOURCE_APP_EXITED` | 关闭自动重启后仅发送SIGTERM，正常cleanup退出；无SIGKILL；PID/容器运行态、TCPsocket和PG/Redis客户端均再次核实。 | 若退出未完成，不开始数据窗口；保留腾讯使用AWS数据服务。 |
| `PG_WINDOW` | 完整quiet、最终LSN、源PG停机fence、target提交/提升、PG端点切换、恢复API均在预算内完成。 | 未接受commit时按第8节中止；已接受commit时仅向腾讯PG恢复。 |
| `PG_TENCENT_REDIS_AWS` | 目标应用PG=腾讯，Redis=AWS；API正常，源PG fence有效，PG角色与重启规格持久。 | 保持混合阶段运行；Redis失败不得把PG回旧端。 |
| `REDIS_WINDOW` | 完整quiet、无FORCE的协调FAILOVER、两端角色核实、target rewrite、Redis端点切换及重建、恢复API完成。 | 角色未变可中止到混合阶段；角色已变或未知时仅按实际主权恢复。 |
| `DATA_TENCENT` | 目标PG/Redis均主端，旧端fenced；新数据端点/缓存/订阅检查通过。 | 只能修复腾讯数据；入口可以经过AWS桥，数据不能回旧AWS。 |
| `VERIFYING` | 恢复目标后台任务、冻结解除前核对计费/使用记录、固定规格重启演练、观察与资源余量通过。 | 保留AWS入口和证据；修复目标。不能用旧快照回退。 |
| `COMPLETE` | 验收证据与至少7天观察安排已保存；AWS仅保留约定的网页服务与可回溯证据。 | 数据退役另行按保留策略；不删除Codex网页服务。 |

旧应用排空不能仅看反向代理请求计数。原应用若没有迁移runtime，必须核实其容器网络命名空间全部业务端口已建立socket为0、没有客户端/代理仍可建立新路由、并取得多次稳定样本。HTTP keepalive复用旧upstream可能延长等待；入口NAT只改变新TCP，不改变已有TCP。若任何直连、私有入口、异步回调或后台实例不能纳入全局writer清单，禁止数据交接。

源码旧应用完全退出后，腾讯迁移应用成为唯一业务进程，且所有后台调度均保持关闭；数据库控制会话及复制会话以白名单身份单列。源应用SIGTERM只发送一次并持久记录；不得使用带默认kill超时的 `docker stop`。停止前连接为0也不等于cleanup完成，须等待进程自然退出且源数据会话归零。

## 6. PostgreSQL单独交接窗口

### 窗口外必须解决的实际启动兼容问题

当前 `prepare_postgres_replica.py` 的启动命令固定 `default_transaction_read_only=on`，每次启动还强制要求 `standby.signal`。直接提升会继续默认只读，随后重启将退出。因此 runner不能直接promote该准备容器。

在该PG尚未被业务访问时，`pg.adopt-promotion-ready` 先导入owner与不可变身份，安装新的主权感知monitor并确认它接管；再停用旧timer、等待旧oneshot自然结束并持久mask旧target timer/service，然后正常停止、替换**仅owned副本容器**，保留同一个已验证数据卷。新启动规格不执行basebackup，也不要求主端存在standby.signal；恢复中依靠PostgreSQL自身强制只读，启动默认read_only允许恢复结束后可写。目标仍在recovery，源保持主端，确认追平及真实重启保持复制后才允许窗口。新容器ID和启动规格更新到root清单，旧prepare cleanup/status不再是新阶段的管理入口。角色未知或未授权的新主只记录告警，不由该准备monitor自动stop。

新monitor的 `STANDBY` 模式验证recovery/read-only；`ARMED` 模式仍不允许意外写主；持久接受的 `PROMOTING` 模式允许同system ID的受控timeline变化；`PRIMARY` 模式验证新timeline、可写、loopback listener和磁盘余量。缺少有效intent的意外promotion仍失败关闭。原target monitor不能留着：它看到新主会主动stop容器。

原source monitor/timer及独立OnFailure fence也必须在源停机前受控退役，等待在途oneshot结束并确认没有pending restart；新source guard接管磁盘、旧主stop/restartpolicy fence和主权检查。不能先停保护再等待很久；有源slot期间持续保持至少一套有效WAL保护。暂停监控不是调用原副本cleanup，不删除副本或卷。

### 正式窗口顺序

1. runner记录窗口绝对deadline并使独立guard接管。调用 `/data/ingress/pause`，保存返回epoch；已经进入业务的HTTP/SSE/WS自然完成。若短时间内不为0，直接中止本次尝试，不碰数据角色。
2. 确认 `active_requests=0`、`active_hijacked_connections=0`、`ingress.quiet=true`，请求 `/data/writers/pause`。核实完整所需writer清单 active/pending0，失败/丢弃统计未新增。
3. 请求 `/data/redis/pause`，停止所有订阅consumer和命令；核实 `quiet=true`、active_operations0。Redis此时仍是AWS主端。本动作是PG quiet guard的依赖，不是Redis角色交接。
4. 请求 `/data/postgres/pause`，核实active_operations0且老池已物理退休；核实AWS PG无业务/未知会话或事务、无prepared transaction和其他写入者。记录 `pg_current_wal_flush_lsn()` 为最终业务LSN；目标replay达到该LSN。值必须数值比较，不按字符串排序。
5. 核实剩余时间足够覆盖源停止、目标提升、端点切换、缓存重建、恢复及guard余量；`pg.arm` 只能接受这一份最终LSN。关闭源PG自动重启和外部管理启动路径，正常/fast shutdown停源PG，不使用SIGKILL或强制checkpoint；核实固定容器已停、RestartPolicy=no、没有旧主listener。源停机后的shutdown WAL不是已确认业务写入的替代LSN证据。
6. 通过固定RPC提交源stop fence证据，`pg.commit` 目标端先fsync intent后执行固定集群提升；再确认system ID一致、timeline前进、recovery=false、业务连接default_transaction_read_only/transaction_read_only均off。未收到响应先 `status/pg.reconcile`，禁止猜测失败后重开源。
7. 更新应用持久启动端点选择为腾讯PG，再通过 `/data/postgres/switch` 选择启动时已经固定的 `target`，使用最新epoch；`/data/postgres/resume`。Redis仍paused时调用 `/data/redis/rebuild`，核实本地缓存与订阅barrier后 `/data/redis/resume`。
8. 核实两数据端点（PG腾讯/RedisAWS）与当前epochs，`/data/writers/resume`，最后 `/data/ingress/resume`。入口恢复必须用本轮epoch，不能用 `/resume` 的drain标记替代。保存最老请求额外等待和窗口耗时，进入混合稳定阶段。

实际控制接口见 `backend/cmd/server/migration_data_wiring.go` 和 `backend/internal/repository/migration_sql_control.go`。阶段读取HTTP状态和JSON含义，不能仅以HTTP200判定quiet；timeout/409可能保持部分fence关闭，必须读取状态后逐项恢复。

## 7. Redis单独交接窗口

窗口外先确认 source `172.18.0.1:26379` 只允许检查过的源Redis容器IP访问，并确实经加密转发到腾讯 `127.0.0.1:26379`、返回本次standby run ID。源的INFO replication报告此advertised地址。不得在窗口中重启SSH服务或修改转发。目标Redis已配置 `/data/migration.conf`，`CONFIG REWRITE`和真实固定镜像重启验证已经通过。

Redis窗口重复入口暂停、HTTP/WS归零、writer fence、Redis pause；PG保持腾讯主端。当前实现的Redis cache rebuild可能读取PG，故无需暂停PG连接池，但PG的业务请求和后台writer必须保持quiet。记录复制身份、两端offset、DB keyspace摘要与TTL样本，不用简单DBSIZE相等替代复制offset。

源执行固定命令 `FAILOVER TO 172.18.0.1 26379 TIMEOUT <审核后的毫秒预算>`，**省略FORCE**。这是Redis协调交接，会暂停旧主写入、追平指定副本并协调角色变化，不能替换为目标单独 `REPLICAOF NO ONE`。发命令前两端记录同一arm，旧source Redis自动重启先禁用。

丢失FAILOVER响应时读取双端ROLE/INFO、failover_state、复制ID/offset和目标ledger。源仍master、目标仍原replica且明确无切换进行中时，才能提交前取消/abort；源已replica、目标已master即不可回旧端。结果未知时不执行FORCE、不发新的相反角色命令，不开放业务写入。

目标成为master后立即 `CONFIG REWRITE` 并核实真实运行配置不含旧replicaof；保留配置文件owner/mode及现有模块兼容启动包装器。持久记录应用Redis默认端点为腾讯，再 `/data/redis/switch` 到固定target，关闭旧连接、完成rebuild和订阅确认，resume Redis、writers、ingress。PG端点仍为腾讯，禁止回source。

旧AWS Redis需要保持已协调降级的replica身份，或正常停止且RestartPolicy=no。若旧容器没有可写config_file，不能宣称其 `CONFIG REWRITE`成功：正常停用，并禁止按旧master启动命令重新启动。若观察期确需开作replica，必须在窗口外另装持久replica配置后重启验证。它不能成为应用回退目标。

目标PG/Redis生产启动配置必须在**放行业务前**已经持久化新角色/端点，避免应用崩溃重新连接旧端。真实数据服务重启验证优先在预演证明；正式提升后的重启属于单独quiet窗口，先暂停入口并归零，不能在有模型流时重启。Redis源复制链路还未完全退役前，不要让其错误重启为旧master。重启验证、资源余量和持久化证据未完成不能宣布全迁移完成。

## 8. 10秒deadline、中止及回退条件

每次窗口开始前读取rehearsal预算表，包括正常步骤的保守上界和**当前阶段到恢复入口**的上界。初始建议在2秒前拿不到完整quiet就中止，在5秒前不能完成所有可回退前置就中止；这只是初值，正式阈值由实际机器演练确定。提交条件为 `elapsed + measured_commit_and_resume_bound + guard_margin < 10s`，不以“估计很快”放行。

两窗口间必须确认上一轮 `queued_requests=0`、受上一轮影响的请求均已完成或已开始正常响应，并保持由完整负载演练确定的最小开放间隔。记录每个请求的稳定request ID、每次admission等待和响应路径中额外暂停，验收取**单请求累计新增等待的最大值**。不能只统计两个窗口各自耗时或平均延迟；若无法证明同一请求不会再次受阻，第二窗口必须从该请求剩余的10秒预算扣除，或继续等待而不启动。0流量也由controller主动检测空队列及间隔后推进。

独立guard观察最老排队开始时刻，不能因重试pause、runner重启、新请求或RPC超时重置deadline。阶段的外部命令timeout必须小于其剩余预算。guard在预留恢复边界触发，而不是等第10秒才开始abort。入口内部不设盲目10秒自动放开，避免放向未知主端。

| 失败时点 | 允许的恢复 | 明确拒绝 |
| --- | --- | --- |
| 任一数据窗口前 | AWS数据保持主端；可撤入口路由，但必须自然排空目标既有连接。DNS失败独立重试。 | 停止仍有连接的源/目标app。 |
| PG尚未接受commit，源PG仍运行 | CAS取消target arm、核实target仍recovery；保持PGsource，按下述逆序恢复。 | 直接放开入口而不恢复data/consumer状态。 |
| PG源已停，但target commit未接受 | target确认cancel成功且不可能迟到commit；确认target仍recovery；仅此时恢复已核实身份的源PG并解除本轮fence，恢复source端点。 | target失联或未知时重启源PG。 |
| PG commit已接受/目标已提升 | 保持源PGstop fence；reconcile并修复腾讯PG/端点/缓存，完成向新主恢复。 | 切回AWSPG、恢复旧快照、运行原副本cleanup。 |
| Redis尚未发生角色变化 | 确认无pending failover并取消arm；仍用当前AWSRedis；PG继续腾讯。 | 把已迁PG一起回AWS。 |
| Redis已完成交接/结果未知 | 保持入口关闭直至双端角色明确；只向腾讯Redis恢复；原AWSRedis保持replica或停止。 | FORCE、单端强制提旧主、把Redis地址改回旧端猜测恢复。 |
| 任一新主已开放业务写入后 | 入口允许AWS桥或腾讯Caddy，但所有数据必须继续新主；修复故障后恢复。 | 从任何旧AWS数据重新承接写入。 |

PG提交前的恢复顺序固定：确认目标未提升且本轮arm已撤销；源PG可写及单主证明；若PG池paused先恢复当前source PG；Redis仍paused时rebuild缓存/订阅并resume Redis；核实两端点；resume writer lease；最后以最新ingress epoch打开入口。Redis提交前中止时PG保持腾讯，同样先修复Redis consumer/cache再writer/ingress。任何一步失败保持其上层fence，不用finally无条件resume。

提交后超过deadline是明确失败事件，独立guard应持续roll-forward并保存不含secret的状态码，而不是停止runner或悄悄续期。所有恢复均使用后台systemd、真实控制请求和固定RPC；不需要模型回复来触发下一步。

## 9. 后台任务、验证和完成标准

两个数据端点都为腾讯、所有quiet lease已正确释放、source app已退出和源数据fence持久后，才激活目标后台调度。激活当前具有单向语义，失败时保留具体组件状态；不能通过环境变量翻转伪造“未运行过”。后台已激活后不得再用先前“scheduler从未启动”的quiet证明做后续交接。

至少需要如下新鲜证据：

- 当前会话停止、0个模型请求时，后台runner仍能完整推进到可服务状态；本机断连不影响远程执行。
- HTTP/SSE/WS覆盖：旧流自然完整结束；新流到腾讯；跨入口变更无丢帧、手动重试或迁移错误；source app仅在连接0后SIGTERM且正常退出。
- 两个独立窗口的正常路径、每个提交前步骤失败/超时、guard抢占、runner进程退出/重启均满足单请求累计额外等待≤10秒；有队列清空、实测开放间隔及跨窗口request ID证据，不能叠加两份10秒。已有长流导致quiet失败时及时中止且流不中断。
- promotion/FAILOVER已成功但响应丢失，恢复逻辑识别新主并只向前完成；没有双主、重复执行反向命令或旧数据写入。
- PG最终LSN包含所有已确认业务写入；切换后计费/余额/使用记录一致，无额外Failed/Dropped；数据库与Redis余量、持久化、网络及API延迟达标。
- 原target standby monitor不会停止新主；source旧monitor/fence没有留有失控重启任务；新主权monitor接管并在角色异常时报告。
- 应用重启保持腾讯PG/Redis端点；PG容器重启不要求standby.signal、不重跑basebackup且仍可写；Redis真实镜像CONFIG REWRITE后重启仍master、数据保留且模块不重复加载。
- Cloudflare token、运行时Bearer、DSN、SSH私钥和Redis凭据没有进入argv、journal或public JSON；RPC拒绝shell、任意资源名、错误owner及过期epoch。
- AWS桥保留覆盖DNS缓存与已有连接；至少7天观察期后再决定数据资源退役。Codex网页服务保持原AWS归属且不纳入本次迁移删除动作。

隔离演练需要在每个状态边界注入故障，并记录源/目标进程与容器前后身份、时间线和角色。正式运行前先审核本设计，再实现/离线测试，再执行隔离端到端演练；不通过时继续AWS现有服务或已完成的安全中间阶段，不进入下一次数据提交。
